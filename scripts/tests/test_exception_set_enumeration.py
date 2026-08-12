"""Stage 10 of smd-act-defects-8: `check-exception-set-enumeration.py`'s resolver.

The plan's own criterion is universally quantified over its prose: every
`--deselect` site must name the same node set, and each site must guard every
node it deselects. This file exercises the STRUCTURAL half (decidable, blocks)
in a fixture domain, and the SEMANTIC half (report-only, always exits 0)
against small synthetic plan files, since the semantic half reads the plan's
raw source text rather than the parsed doc. The check that THIS plan's own
`--deselect` sites resolve runs against the committed snapshot
(`fixtures/plan_snapshot_smd-act-defects-8.toml`) both here and as a direct CLI
invocation in this stage's own `verify_command`, on the same two-places
rationale `test_order_coverage_map.py` states for the order-coverage resolver.
"""
import importlib.util
import sys
from pathlib import Path

from agentctl.plan import parse_plan

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "check_exception_set_enumeration", ROOT / "scripts" / "check-exception-set-enumeration.py"
)
check_exception_set_enumeration = importlib.util.module_from_spec(_SPEC)
sys.modules["check_exception_set_enumeration"] = check_exception_set_enumeration
_SPEC.loader.exec_module(check_exception_set_enumeration)

structural_violations = check_exception_set_enumeration.structural_violations
main = check_exception_set_enumeration.main


def _stage_dict(index=1, **overrides):
    base = {
        "index": index, "title": f"s{index}", "executor": "in_thread",
        "expected_result_image": "img", "done_criterion": "dc",
        "means": "Edit", "method": "do", "verify_command": "true",
    }
    base.update(overrides)
    return base


def _doc(stages, final_check=None):
    data = {"meta": {"task_id": "t"}, "stage": stages}
    if final_check is not None:
        data["final_check"] = final_check
    return parse_plan(data)


def _guarded_command(nodes, eq="1", deselect_sep=" "):
    """The plan's own guarded-deselect shell idiom: each node gets its own
    single-node invocation whose exit status is captured into a variable and
    compared to `eq`, the comparisons `&&`-chained into a single run
    deselecting every node. Node ids carry `::` because the resolver counts
    only a single-node invocation as a guard."""
    captures = []
    checks = []
    for i, node in enumerate(nodes):
        var = chr(ord("a") + i)
        captures.append(f"python3 -m pytest {node} -q > /dev/null 2>&1; {var}=$?;")
        checks.append(f"test ${var} -eq {eq}")
    deselects = " ".join(f"--deselect{deselect_sep}{node}" for node in nodes)
    return (
        "python3 -m pytest scripts/tests -q || { "
        + " ".join(captures) + " " + " && ".join(checks)
        + " && python3 -m pytest scripts/tests -q " + deselects + "; }"
    )


# --- structural half ---------------------------------------------------------

def test_sites_that_agree_are_clean():
    cmd = _guarded_command(["f.py::NODE_A", "f.py::NODE_B"])
    doc = _doc([_stage_dict(1, verify_command=cmd)], final_check=[{"command": cmd}])
    assert structural_violations(doc) == []


def test_a_site_free_plan_is_clean():
    doc = _doc([_stage_dict(1)])
    assert structural_violations(doc) == []


def test_sites_that_disagree_are_reported():
    cmd1 = _guarded_command(["f.py::NODE_A"])
    cmd2 = _guarded_command(["f.py::NODE_A", "f.py::NODE_B"])
    doc = _doc([_stage_dict(1, verify_command=cmd1), _stage_dict(2, verify_command=cmd2)])
    violations = structural_violations(doc)
    assert any("disagrees with the union" in v for v in violations)


def test_a_two_node_site_deselecting_an_unguarded_node_is_reported():
    # NODE_B is properly guarded; NODE_A is deselected with no guard evidence
    # at all — the load-bearing case an implementation scanning the whole
    # command string for a bare "-eq 1" would wrongly pass.
    command = (
        "python3 -m pytest scripts/tests -q || { "
        "python3 -m pytest f.py::NODE_B -q > /dev/null 2>&1; b=$?; "
        "test $b -eq 1 && "
        "python3 -m pytest scripts/tests -q --deselect f.py::NODE_A --deselect f.py::NODE_B; }"
    )
    doc = _doc([_stage_dict(1, verify_command=command)])
    violations = structural_violations(doc)
    assert any("NODE_A" in v and "without a guard" in v for v in violations)
    assert not any("NODE_B" in v and "without a guard" in v for v in violations)


def test_a_site_guarding_a_node_it_does_not_deselect_is_reported():
    # NODE_X is captured and compared to 1 but never appears after --deselect;
    # NODE_Y is a proper guarded deselect, isolating the harmless-inverse case.
    command = (
        "python3 -m pytest scripts/tests -q || { "
        "python3 -m pytest f.py::NODE_X -q > /dev/null 2>&1; a=$?; "
        "python3 -m pytest f.py::NODE_Y -q > /dev/null 2>&1; b=$?; "
        "test $a -eq 1 && test $b -eq 1 && "
        "python3 -m pytest scripts/tests -q --deselect f.py::NODE_Y; }"
    )
    doc = _doc([_stage_dict(1, verify_command=command)])
    violations = structural_violations(doc)
    assert any("NODE_X" in v and "never deselects it" in v for v in violations)


def test_a_guard_comparing_to_zero_is_not_a_guard():
    doc = _doc([_stage_dict(1, verify_command=_guarded_command(["f.py::NODE_A"], eq="0"))])
    violations = structural_violations(doc)
    assert any("NODE_A" in v and "without a guard" in v for v in violations)


def test_a_final_check_site_deselecting_an_unguarded_node_is_reported():
    # The stage's own site is clean, so only the final_check limb of _sites can
    # surface this — without that limb half the site domain goes unread.
    doc = _doc(
        [_stage_dict(1, verify_command=_guarded_command(["f.py::NODE_A"]))],
        final_check=[
            {"command": "python3 -m pytest scripts/tests -q --deselect f.py::NODE_A"}
        ],
    )
    violations = structural_violations(doc)
    assert any("final_check 1" in v and "without a guard" in v for v in violations)


def test_an_unreadable_deselect_spelling_is_a_violation_naming_the_site():
    # A `--deselect` this resolver cannot read must fail rather than skip:
    # skipping would leave the site naming an empty node set that agrees with
    # every other empty one, and report a clean plan.
    command = "python3 -m pytest scripts/tests -q --deselect"
    doc = _doc([_stage_dict(1, verify_command=command)])
    violations = structural_violations(doc)
    assert any("stage 1" in v and "never a silent skip" in v for v in violations)


def test_the_equals_spelling_is_read_as_a_node():
    cmd = _guarded_command(["f.py::NODE_A"], deselect_sep="=")
    doc = _doc([_stage_dict(1, verify_command=cmd)])
    assert structural_violations(doc) == []


def test_a_guard_a_semicolon_separates_from_the_deselecting_run_is_not_a_guard():
    # The comparison textually precedes the deselecting run, but its result is
    # discarded: the run executes whether or not NODE_A came back red.
    command = (
        "python3 -m pytest f.py::NODE_A -q > /dev/null 2>&1; a=$?; "
        "test $a -eq 1; "
        "python3 -m pytest scripts/tests -q --deselect f.py::NODE_A"
    )
    doc = _doc([_stage_dict(1, verify_command=command)])
    violations = structural_violations(doc)
    assert any("NODE_A" in v and "without a guard" in v for v in violations)


def test_a_guard_invocation_after_the_deselecting_run_does_not_count():
    # The comparison is `&&`-chained into the deselecting run, so only the
    # slice that stops the invocation scan at the first --deselect can see
    # that the invocation establishing `a` has not run yet.
    command = (
        "python3 -m pytest scripts/tests -q || { "
        "test $a -eq 1 && python3 -m pytest scripts/tests -q --deselect f.py::NODE_A; "
        "python3 -m pytest f.py::NODE_A -q > /dev/null 2>&1; a=$?; }"
    )
    doc = _doc([_stage_dict(1, verify_command=command)])
    violations = structural_violations(doc)
    assert any("NODE_A" in v and "without a guard" in v for v in violations)


def test_a_whole_directory_invocation_is_not_a_guarded_node():
    # `scripts/tests` is not a node id; registering it would trip the
    # guards-but-never-deselects branch on a correct plan.
    command = (
        "python3 -m pytest scripts/tests -q > /dev/null 2>&1; rc=$?; "
        "test $rc -eq 1 && "
        "python3 -m pytest scripts/tests -q --deselect f.py::NODE_A"
    )
    doc = _doc([_stage_dict(1, verify_command=command)])
    violations = structural_violations(doc)
    assert not any("never deselects it" in v for v in violations)


# --- semantic half: report-only, always exits 0 -------------------------------

def test_semantic_half_reports_a_cardinality_sentence_and_exits_zero(tmp_path, capsys):
    plan_path = tmp_path / "prose.toml"
    plan_path.write_text(
        '[meta]\ntask_id = "t"\n'
        '[[stage]]\n'
        'index = 1\ntitle = "s"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\nverify_command = "true"\n'
        'conditions = "The exception set names two known-red tests, both still red."\n',
        encoding="utf-8",
    )
    exit_code = main(["check-exception-set-enumeration.py", str(plan_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "The exception set names two known-red tests, both still red." in out


def test_semantic_half_never_flips_a_structural_failure_to_pass(tmp_path):
    plan_path = tmp_path / "prose_and_broken.toml"
    plan_path.write_text(
        '[meta]\ntask_id = "t"\n'
        '[[stage]]\n'
        'index = 1\ntitle = "s"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\n'
        'verify_command = "python3 -m pytest scripts/tests -q --deselect NODE_A"\n'
        'conditions = "The exception set names one known-red test."\n',
        encoding="utf-8",
    )
    assert main(["check-exception-set-enumeration.py", str(plan_path)]) == 1


def test_semantic_half_flags_a_bare_definite_singular_reference_without_a_cardinality_word(
    tmp_path, capsys
):
    # No digit or number word anywhere in this sentence — only the vocabulary
    # word "excluded" plus the bare "the excluded node" reference is what must
    # trigger the hit, isolating _DEFINITE_SINGULAR_RE from _CARDINALITY_RE.
    plan_path = tmp_path / "definite.toml"
    plan_path.write_text(
        '[meta]\ntask_id = "t"\n'
        '[[stage]]\n'
        'index = 1\ntitle = "s"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\nverify_command = "true"\n'
        'conditions = "The excluded node must stay red until trunk repairs it."\n',
        encoding="utf-8",
    )
    exit_code = main(["check-exception-set-enumeration.py", str(plan_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "The excluded node must stay red until trunk repairs it." in out


def test_semantic_half_labels_a_stage_subtable_sentence_with_its_stage_index(tmp_path, capsys):
    # [stage.principle] is a dotted subtable of the array element most recently
    # opened by [[stage]] — losing the ordinal here would leave a hit
    # indistinguishable from the same field on any other stage.
    plan_path = tmp_path / "subtable.toml"
    plan_path.write_text(
        '[meta]\ntask_id = "t"\n'
        '[[stage]]\n'
        'index = 3\ntitle = "s"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\nverify_command = "true"\n'
        '[stage.principle]\n'
        'statement = "The exception set names two known-red tests, both still red."\n'
        'source = "s"\nderivation = "d"\nconfidence = "high"\nrefutation = "r"\n',
        encoding="utf-8",
    )
    exit_code = main(["check-exception-set-enumeration.py", str(plan_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "stage 3.principle.statement" in out


def test_semantic_half_labels_a_final_check_sentence_with_its_ordinal(tmp_path, capsys):
    # final_check is an array of tables with no index field of its own (unlike
    # stage) — the ordinal is the block's 1-based position, matching the
    # structural half's own "final_check N" site label.
    plan_path = tmp_path / "finalcheck.toml"
    plan_path.write_text(
        '[meta]\ntask_id = "t"\n'
        '[[stage]]\n'
        'index = 1\ntitle = "s"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\nverify_command = "true"\n'
        '[[final_check]]\n'
        'command = "true"\n'
        'label = "The exception set names two known-red tests, both still red."\n',
        encoding="utf-8",
    )
    exit_code = main(["check-exception-set-enumeration.py", str(plan_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "final_check 1.label" in out


def test_a_long_candidate_sentence_is_printed_untruncated(tmp_path, capsys):
    # A prior version of this check cut candidates at 300 characters, and a
    # cardinality word falling past the cutoff let a reader clear a site on
    # half a sentence — so here the operative words sit past 350.
    filler = "this clause names no suite, no stage and no check, and runs on, " * 6
    sentence = filler + "and only at its end does it name the two known-red tests."
    assert len(filler) > 350
    plan_path = tmp_path / "long.toml"
    plan_path.write_text(
        '[meta]\ntask_id = "t"\n'
        '[[stage]]\n'
        'index = 1\ntitle = "s"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\nverify_command = "true"\n'
        f'conditions = "{sentence}"\n',
        encoding="utf-8",
    )
    exit_code = main(["check-exception-set-enumeration.py", str(plan_path)])
    assert exit_code == 0
    assert sentence in capsys.readouterr().out


def test_the_sweep_raising_changes_neither_verdict(tmp_path, capsys, monkeypatch):
    # Report-only has to hold on every path, not merely on every reading: a
    # failure inside the sweep must not become an exit code of its own, nor
    # suppress a structural failure already computed.
    def boom(_plan_path):
        raise OSError("simulated read failure")

    monkeypatch.setattr(check_exception_set_enumeration, "semantic_report", boom)
    header = (
        '[meta]\ntask_id = "t"\n'
        '[[stage]]\n'
        'index = 1\ntitle = "s"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n'
        'means = "Edit"\nmethod = "do"\n'
    )
    clean = tmp_path / "clean.toml"
    clean.write_text(header + 'verify_command = "true"\n', encoding="utf-8")
    assert main(["check-exception-set-enumeration.py", str(clean)]) == 0
    assert "unavailable" in capsys.readouterr().out

    broken = tmp_path / "broken.toml"
    broken.write_text(
        header + 'verify_command = "python3 -m pytest scripts/tests -q --deselect f.py::NODE_A"\n',
        encoding="utf-8",
    )
    assert main(["check-exception-set-enumeration.py", str(broken)]) == 1


# --- main(): CLI exit codes -----------------------------------------------

def test_main_exits_two_on_bad_usage():
    assert main(["check-exception-set-enumeration.py"]) == 2


def test_main_exits_one_on_a_missing_file():
    assert main(["check-exception-set-enumeration.py", "/nonexistent/plan.toml"]) == 1


def test_main_resolves_against_the_committed_smd_act_defects_8_snapshot():
    snapshot = Path(__file__).parent / "fixtures" / "plan_snapshot_smd-act-defects-8.toml"
    assert main(["check-exception-set-enumeration.py", str(snapshot)]) == 0
