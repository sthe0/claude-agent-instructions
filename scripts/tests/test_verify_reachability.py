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
    # local plan artifact is absent (other checkouts / CI).
    plan_path = config_root.plans_dir() / "question-provenance-and-derivation.toml"
    if not plan_path.exists():
        pytest.skip("plan artifact not present in this checkout")
    doc = load_plan(str(plan_path))
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

[[stage]]
index = 1
title = "Produce a file"
executor = "in_thread"
expected_result_image = "i"
criterion_type = "measurable"
done_criterion = "d"
material = "m"
means = "me"
method = "mt"
invariants = "inv"
capability_required = "c"
conditions = "co"
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
