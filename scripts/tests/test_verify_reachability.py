"""Two-directional control, GREEN side (stage 8): a verify_command / final_check
is trusted only when its green direction is REACHABLE — every literal repo path
it names either already exists or is produced by some stage (declared in that
stage's output_artifacts). The scope lint (test_verify_command_scope.py) covers
the RED side (a control must not be false-failed); this covers the GREEN side (a
control must be able to pass honestly). Unlike scope, reachability has no
legitimate counter-instance, so it BLOCKS a substantive plan rather than warning.
"""
from argparse import Namespace
from dataclasses import asdict
from pathlib import Path

import pytest

from lib import config_root
from agentctl import cli
from agentctl.plan import (
    load_plan,
    parse_plan,
    verify_command_reachability_blockers,
    _reachability_path_tokens,
)
from agentctl.state import Stage
from agentctl.store import FileStateStore

from conftest import SUBSTANTIVE_ORDER


def ns(**kw):
    return Namespace(**kw)


def _stage(index=1, title="s", verify_command="python3 -m agentctl status",
           output_artifacts=None):
    # Default verify_command has no literal path token, so a stage exercising
    # output_artifacts parsing (not reachability) is itself reachability-clean.
    d = {
        "index": index,
        "title": title,
        "executor": "in_thread",
        "expected_result_image": "i",
        "criterion_type": "measurable",
        "done_criterion": "d",
        "material": "m",
        "means": "me",
        "method": "mt",
        "invariants": "inv",
        "capability_required": "c",
        "conditions": "co",
        "verify_command": verify_command,
        "principle": {
            "statement": "s", "source": "src",
            "derivation": "a distinct derivation clause",
            "confidence": "high", "refutation": "r",
        },
    }
    if output_artifacts is not None:
        d["output_artifacts"] = output_artifacts
    return d


def _doc(stages, repo_root=None, final_check=None):
    meta = {"task_id": "t", "weight_class": "substantive", "external_research": "n/a"}
    if repo_root is not None:
        meta["repo_root"] = repo_root
    data = {"meta": meta, "stage": stages}
    if final_check is not None:
        data["final_check"] = final_check
    return parse_plan(data)


def _blockers(stages, repo_root=None, final_check=None):
    doc = _doc(stages, repo_root=repo_root, final_check=final_check)
    return verify_command_reachability_blockers(
        doc.stages, doc.meta.final_check, doc.meta.repo_root
    )


# --- a0: output_artifacts is a real, tolerant, round-tripped field ----------

_ROUND_TRIP_TOML = """
[meta]
task_id = "rt"
weight_class = "substantive"
external_research = "n/a"

[[stage]]
index = 1
title = "declares artifacts"
executor = "in_thread"
expected_result_image = "i"
criterion_type = "measurable"
done_criterion = "d"
material = "m"
means = "me"
method = "mt"
procedure = "1. read the fixture. 2. apply the edit. 3. re-check the seam"
invariants = "inv"
capability_required = "c"
conditions = "co"
verify_command = "pytest -q"
output_artifacts = ["scripts/agentctl/new.py", "scripts/tests/"]
[stage.principle]
statement = "s"
source = "src"
derivation = "a distinct derivation clause"
confidence = "high"
refutation = "r"
"""


def test_output_artifacts_round_trip_through_load_plan(tmp_path):
    # Through the SAME parser the engine uses — not a bare from_dict — the field
    # must survive, or the reachability rule that reads it has nothing to read.
    p = tmp_path / "plan.toml"
    p.write_text(_ROUND_TRIP_TOML, encoding="utf-8")
    doc = load_plan(str(p))
    assert doc.stages[0].output_artifacts == ["scripts/agentctl/new.py", "scripts/tests/"]
    # asdict/from_dict identity too — the field is real on the dataclass.
    rebuilt = Stage.from_dict(asdict(doc.stages[0]))
    assert rebuilt.output_artifacts == ["scripts/agentctl/new.py", "scripts/tests/"]


def test_plan_without_output_artifacts_still_loads():
    # Tolerant/optional: a plan omitting the key loads exactly as before, with an
    # empty list — never an error, or every legacy plan on disk would break.
    doc = _doc([_stage()])  # key omitted entirely
    assert doc.stages[0].output_artifacts == []


# --- tokenizer: only literal relative path-shaped tokens count --------------

def test_path_inside_quoted_dash_c_arg_is_not_flagged():
    # shlex collapses the quoted program body into ONE token that the -c drop
    # discards, even though the body contains `/`-shaped substrings and `;`.
    assert _reachability_path_tokens("python3 -m agentctl status") == []
    assert _reachability_path_tokens(
        'python3 -c "import agentctl.plan; open(\'scripts/x.py\')"'
    ) == []


def test_token_with_whitespace_is_not_a_path():
    # A shlex token that still contains whitespace (a quoted human string) is prose,
    # not a path — even if it ends in a path-like extension it must never be probed.
    assert _reachability_path_tokens('echo "a sentence with spaces.py"') == []
    assert _reachability_path_tokens("cmd 'multi word arg/thing.py'") == []


def test_variable_path_is_skipped():
    # A path built from a shell variable / substitution is not a literal — skipped,
    # not checked. This is the named limit: the lint is defeated by $(...) and $VAR.
    assert _reachability_path_tokens("cat $HOME/x.txt") == []
    assert _reachability_path_tokens("python3 ${DIR}/run.py") == []


def test_absolute_path_outside_repo_root_is_skipped():
    assert _reachability_path_tokens("cmd > /dev/null") == []
    assert _reachability_path_tokens("test -f /tmp/scratch.txt") == []


def test_pytest_node_id_tail_is_stripped():
    assert _reachability_path_tokens("pytest t/test_x.py::test_y") == ["t/test_x.py"]


# --- tokenizer: shell-syntax false positives (stage 2) -----------------------

def test_negated_existence_check_is_exempt():
    # `! test -f P` / `! [ -f P ]` assert ABSENCE -- demanding P be producible
    # is exactly backwards. Real shape from the frozen v23 reference plan, stage 3.
    assert _reachability_path_tokens(
        "! test -f library/svc/data_science/Dockerfile"
    ) == []
    assert _reachability_path_tokens(
        "! [ -f library/svc/data_science/Dockerfile ]"
    ) == []


def test_negation_only_exempts_test_and_bracket_forms():
    # `! <anything but test/[> P` is NOT exempt -- narrowed to the two forms.
    assert _reachability_path_tokens(
        "! grep -q PAT scripts/ghost.py"
    ) == ["scripts/ghost.py"]


def test_negation_operand_position_test_form_is_exempt():
    # POSIX operand-position spelling: `!` is `test`'s FIRST operand, not its
    # predecessor -- same absence assertion as `! test -f P`, different syntax.
    assert _reachability_path_tokens(
        "test ! -f library/svc/data_science/Dockerfile"
    ) == []


def test_negation_operand_position_bracket_form_is_exempt():
    assert _reachability_path_tokens(
        "[ ! -f library/svc/data_science/Dockerfile ]"
    ) == []


def test_negation_operand_position_only_when_bang_is_first_operand():
    # A `!` anywhere but the very first operand of `test`/`[` does not arm the
    # exemption -- both paths here must still block.
    assert _reachability_path_tokens(
        "test -f scripts/ghost.py -a ! -f scripts/other.py"
    ) == ["scripts/ghost.py", "scripts/other.py"]


def test_bracket_close_ends_negated_clause():
    # `]` is the one clause terminator (B) matches directly (`_CLOSING_BRACKET`);
    # `&&`/`||`/`;`/`|` end the clause via the trailing-glue reset instead, since
    # a token made entirely of `_GLUE_CHARS` collapses to "" before that lookup
    # ever runs (see the constant's module comment) -- this pins the one case
    # that is NOT glue-driven. The two clauses are separated by a bare newline
    # (not `&&`) so no `_GLUE_CHARS`-only token is ever produced -- if
    # `_CLOSING_BRACKET` were emptied, nothing else in the state machine would
    # end the negated clause and ghost.py would wrongly stay exempt.
    assert _reachability_path_tokens(
        "[ ! -f library/svc/data_science/Dockerfile ]\ntest -f scripts/ghost.py"
    ) == ["scripts/ghost.py"]


def test_clause_break_glue_is_stripped():
    # shlex never splits `P;` inside `for F in ... P; do ...` -- real shape from
    # the frozen v23 reference plan, stage 5 / final_check 4.
    assert _reachability_path_tokens(
        "for F in pkg.json library/svc/data_science/docker_package.json; "
        'do test -f "$F" || exit 1; done'
    ) == ["pkg.json", "library/svc/data_science/docker_package.json"]


def test_orphan_path_with_no_glue_still_blocks():
    assert _reachability_path_tokens("test -f scripts/ghost.py; echo done") == [
        "scripts/ghost.py"
    ]


def test_grep_pattern_argument_is_not_a_path():
    # Real shape from the frozen v23 reference plan, stage 7: a regex pattern operand that is
    # `/`-shaped and looks exactly like a path.
    assert _reachability_path_tokens(
        "grep -qE '(review|pull)/15149870' /tmp/evidence/pr.md"
    ) == []


def test_grep_pattern_argument_file_operand_still_checked():
    # The pattern is skipped; a real FILE operand after it must still block.
    assert _reachability_path_tokens("grep -q PAT scripts/ghost.py") == [
        "scripts/ghost.py"
    ]


def test_reject_chars_narrowed_to_open_paren_and_pipe():
    # `)` was dropped from `_REJECT_CHARS` (code review, see the constant's
    # module comment): a real path containing `)` but not `(` is now checked,
    # unlike before this fix.
    assert _reachability_path_tokens("test -f scripts/gh)ost.py") == [
        "scripts/gh)ost.py"
    ]


def test_reject_chars_still_excludes_open_paren_and_pipe():
    # `(` and `|` stay in `_REJECT_CHARS` as defence in depth for a
    # grep-family pattern (D)'s positional recognition fails to catch --
    # here there is no grep command word at all, so (D) never even looks.
    assert _reachability_path_tokens('test -f "(group)/thing.py"') == []
    assert _reachability_path_tokens('test -f "a|b.py"') == []


def test_heredoc_body_is_not_shell_argv():
    # Real shape from the frozen v23 reference plan, stage 10: a `python3 - <<'TAG'` body is
    # Python source, not shell tokens -- `print("ci/tests:", ...)` must not
    # surface as the bogus path-shaped token `print(ci/tests:,`.
    cmd = (
        "python3 - <<'PYJUDGE'\n"
        'print("ci/tests:", 1)\n'
        "PYJUDGE\n"
    )
    assert _reachability_path_tokens(cmd) == []


def test_orphan_path_before_heredoc_still_blocks():
    cmd = (
        "test -f scripts/ghost.py && python3 - <<'PYJUDGE'\n"
        'print("ci/tests:", 1)\n'
        "PYJUDGE\n"
    )
    assert _reachability_path_tokens(cmd) == ["scripts/ghost.py"]


def test_byproduct_path_is_exempt():
    # Real shape from the frozen v23 reference plan, stages 5/8/final_check:
    # `scripts/ya_test_textlog.py` reads this trace path only AFTER the `ya
    # make` run it itself launches has WRITTEN it -- a byproduct of the
    # checked command's own execution, not a precondition. The leading
    # `scripts/ya_test_textlog.py` operand is a real, unrelated path (the
    # script itself) and is untouched by (E) -- only the trace argument is
    # dropped.
    assert _reachability_path_tokens(
        "python3 scripts/ya_test_textlog.py . /tmp/x.log "
        "library/svc/data_science/tests/test-results/py3test/ytest.report.trace -tt"
    ) == ["scripts/ya_test_textlog.py"]


def test_orphan_path_with_no_build_output_segment_still_blocks():
    # Same argument position, minus the build-output segment -- (E) must not
    # swallow an ordinary precondition path just because it sits where a
    # byproduct path could also sit.
    assert _reachability_path_tokens(
        "python3 scripts/ya_test_textlog.py . /tmp/x.log "
        "library/svc/data_science/tests/results/py3test/ytest.report.trace -tt"
    ) == ["scripts/ya_test_textlog.py",
          "library/svc/data_science/tests/results/py3test/ytest.report.trace"]


def _ya_test_textlog_repo(tmp_path):
    # The script path itself is a real, pre-existing repo file in every real
    # invocation of this shape; create it so these end-to-end cases isolate
    # the assertion to the trace-path argument (E) actually governs, rather
    # than tripping on an unrelated, incidental blocker.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ya_test_textlog.py").write_text("x", encoding="utf-8")
    return tmp_path


def test_byproduct_path_exempt_end_to_end_even_when_nonexistent(tmp_path):
    # Environment-independent proof that (E) drops the token BEFORE
    # reachability is ever consulted, not that the path happens to already
    # exist on this machine's build cache: the trace path itself is nowhere
    # on disk and nothing declares it, yet it still does not block.
    root = _ya_test_textlog_repo(tmp_path)
    assert _blockers(
        [_stage(verify_command=(
            "python3 scripts/ya_test_textlog.py . /tmp/x.log "
            "library/svc/data_science/tests/test-results/py3test/ytest.report.trace -tt"
        ))],
        repo_root=str(root),
    ) == []


def test_orphan_path_without_build_output_segment_blocks_end_to_end(tmp_path):
    root = _ya_test_textlog_repo(tmp_path)
    b = _blockers(
        [_stage(verify_command=(
            "python3 scripts/ya_test_textlog.py . /tmp/x.log "
            "library/svc/data_science/tests/results/py3test/ytest.report.trace -tt"
        ))],
        repo_root=str(root),
    )
    assert len(b) == 1
    assert "library/svc/data_science/tests/results/py3test/ytest.report.trace" in b[0]


# --- interaction cases: two mechanisms meeting at one boundary ---------------

def test_negation_dedup_does_not_exempt_the_later_positive_occurrence():
    # `_check`'s per-command `seen` set dedups by TOKEN. If (B) recorded the
    # path in a command-wide exempt set instead of just declining to EMIT this
    # occurrence, the later, genuinely-checkable positive occurrence of the
    # SAME path would be silently exempted too.
    assert _reachability_path_tokens(
        "! test -f scripts/ghost.py && test -f scripts/ghost.py"
    ) == ["scripts/ghost.py"]


def test_orphan_path_immediately_after_heredoc_terminator_still_blocks():
    # The shape the per-category pairs omit: an orphan path AFTER the
    # here-document, not before it. An off-by-one that swallows the terminator
    # line's successor would make the lint blind to everything past a heredoc.
    cmd = (
        "python3 - <<'PY'\n"
        'print("a/b.py")\n'
        "PY\n"
        "&& test -f scripts/ghost.py"
    )
    assert _reachability_path_tokens(cmd) == ["scripts/ghost.py"]


def test_negation_exemption_ends_at_glued_clause_break():
    # `! test -f foo.txt;` -- the negation exemption must end exactly at the
    # `;` glued onto foo.txt (the (C)-before-(B) ordering), so ghost.py in the
    # NEXT clause is not swept in as exempt too.
    assert _reachability_path_tokens(
        "true && ! test -f foo.txt; test -f scripts/ghost.py"
    ) == ["scripts/ghost.py"]


# --- reachability decision --------------------------------------------------

def test_existing_repo_path_is_reachable(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "there.py").write_text("x", encoding="utf-8")
    assert _blockers(
        [_stage(verify_command="pytest scripts/there.py")], repo_root=str(tmp_path)
    ) == []


def test_path_declared_in_output_artifacts_is_reachable(tmp_path):
    stages = [
        _stage(index=1, output_artifacts=["scripts/made.py"]),
        _stage(index=2, verify_command="pytest scripts/made.py"),
    ]
    assert _blockers(stages, repo_root=str(tmp_path)) == []


def test_declared_directory_prefix_covers_file_under_it(tmp_path):
    stages = [
        _stage(index=1, output_artifacts=["scripts/agentctl/"]),
        _stage(index=2, verify_command="pytest scripts/agentctl/new.py"),
    ]
    assert _blockers(stages, repo_root=str(tmp_path)) == []


def test_orphan_path_blocks(tmp_path):
    b = _blockers(
        [_stage(verify_command="pytest scripts/ghost.py")], repo_root=str(tmp_path)
    )
    assert len(b) == 1
    assert "scripts/ghost.py" in b[0]


def test_blocker_names_both_routes_out(tmp_path):
    (msg,) = _blockers(
        [_stage(verify_command="pytest scripts/ghost.py")], repo_root=str(tmp_path)
    )
    assert "create the file" in msg
    assert "output_artifacts" in msg


def test_final_check_path_nothing_produces_is_blocked(tmp_path):
    b = _blockers(
        [_stage(verify_command="pytest -q", output_artifacts=[])],
        repo_root=str(tmp_path),
        final_check=[{"command": "test -f scripts/absent.py", "label": "smoke"}],
    )
    # `test -f` uses no absolute path; scripts/absent.py is relative + orphan
    assert any("scripts/absent.py" in x and "final_check" in x for x in b)


def test_this_plans_own_controls_are_all_reachable():
    # Dogfood: THIS plan, loaded through the engine's own load_plan, must carry ZERO
    # reachability blockers — every path its controls name exists in the tree or is
    # declared in a stage's output_artifacts. The rule was run against this plan
    # while it was written; this pins that it stays true. Skips where the machine-
    # local plan artifact is absent (other checkouts / CI) or where the venue it
    # pins is gone — reachability is resolved against meta.repo_root, so a removed
    # worktree makes every path unreachable for a reason that is not the plan's.
    plan_path = config_root.plans_dir() / "question-provenance-and-derivation.toml"
    if not plan_path.exists():
        pytest.skip("plan artifact not present in this checkout")
    doc = load_plan(str(plan_path))
    if not Path(doc.meta.repo_root).is_dir():
        pytest.skip("the plan's repo_root venue is not present on this machine")
    b = verify_command_reachability_blockers(
        doc.stages, doc.meta.final_check, doc.meta.repo_root
    )
    assert b == [], f"this plan's own controls must all be reachable, got: {b}"


def test_docstring_names_false_positives_and_the_two_limits():
    import agentctl.plan as plan_mod
    src = __import__("inspect").getsource(plan_mod)
    lowered = src.lower()
    assert "false positive" in lowered
    assert "reachability is not validity" in lowered
    assert "green-reachab" in lowered


# --- integration through cmd_submit_plan (gated to substantive) -------------

def _session(store, sid, tmp_path, *, architectural):
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False,
                        changed_lines=200 if architectural else 5,
                        files=5 if architectural else 1,
                        wall_clock_min=60 if architectural else 5,
                        tracker_key=None, architectural=architectural,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    if architectural:
        cli.cmd_plan(ns(session=sid), store=store)


_PLAN_TMPL = """
[meta]
task_id = "demo"
goal = "g"
done_criterion = "dc"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "n/a"
repo_root = "{root}"
""" + SUBSTANTIVE_ORDER + """
[[stage]]
index = 1
title = "Produce a file"
executor = "in_thread"
expected_result_image = "i"
criterion_type = "measurable"
done_criterion = "d"
material = "m"
material_refs = ["m"]
knowledge_refs = ["k"]
knowledge = "kn"
means = "me"
method = "mt"
procedure = "1. read the fixture. 2. apply the edit. 3. re-check the seam"
invariants = "inv"
capability_required = "c"
conditions = "co"
preconditions = "p"
{artifacts}verify_command = "pytest -q"

[stage.principle]
statement = "s"
source = "src"
derivation = "a distinct derivation clause"
confidence = "high"
refutation = "r"

[[final_check]]
command = "{fccmd}"
label = "smoke"
"""


def test_submit_plan_refuses_unreachable_final_check(tmp_path):
    # A substantive plan whose FINAL_CHECK names a path no stage produces is refused;
    # the refusal stays at PLANNING (gate not armed) and names the offending path.
    store = FileStateStore(tmp_path / "state")
    sid = "reach-fc"
    _session(store, sid, tmp_path, architectural=True)
    plan = tmp_path / "plan.toml"
    plan.write_text(
        _PLAN_TMPL.format(root=str(tmp_path), artifacts="",
                          fccmd="test -f scripts/ghost.py"),
        encoding="utf-8",
    )
    d = cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)
    assert d.ok is False
    assert d.action == "fix_plan"
    assert d.node == "PLANNING"  # stays put; gate not armed on a failed check
    problems = d.data.get("problems", [])
    assert any("scripts/ghost.py" in p and "final_check" in p for p in problems)


def test_submit_plan_passes_when_final_check_path_declared(tmp_path):
    # Declare the path in a stage's output_artifacts and the same plan passes —
    # the GREEN direction of the same control (two-directional discipline on the lint).
    store = FileStateStore(tmp_path / "state")
    sid = "reach-fc-pass"
    _session(store, sid, tmp_path, architectural=True)
    plan = tmp_path / "plan.toml"
    plan.write_text(
        _PLAN_TMPL.format(root=str(tmp_path),
                          artifacts='output_artifacts = ["scripts/ghost.py"]\n',
                          fccmd="test -f scripts/ghost.py"),
        encoding="utf-8",
    )
    d = cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)
    assert d.ok is True
    assert d.marker == "PLAN-READY"


def test_nonsubstantive_plan_is_not_gated(tmp_path):
    # Reachability BLOCKS only substantive plans (method (b)); a small-change session
    # submitting a plan that names an orphan path must NOT be gated on reachability.
    store = FileStateStore(tmp_path / "state")
    sid = "reach-small"
    _session(store, sid, tmp_path, architectural=False)
    # Drive to PLANNING so submit-plan is reachable, but the session weight stays
    # small_change — the point under test is that the reachability block keys off
    # weight_class, not off the plan's own meta (which the template marks substantive).
    cli.cmd_plan(ns(session=sid), store=store)
    plan = tmp_path / "plan.toml"
    plan.write_text(
        _PLAN_TMPL.format(root=str(tmp_path), artifacts="",
                          fccmd="test -f scripts/ghost.py"),
        encoding="utf-8",
    )
    d = cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)
    problems = (d.data or {}).get("problems", [])
    assert not any("ghost" in p for p in problems), problems
