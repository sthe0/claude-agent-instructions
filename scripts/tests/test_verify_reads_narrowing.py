"""verify_reads: an opt-in narrowed lookup in verify-final's session green-check cache.

A check that declares `verify_reads` (the repo paths it reads, [] for none) asserts it
reads no git history. After an exact cache miss, verify-final then looks for an earlier
green of the same command in the same venue and serves the check from it ("narrowed")
only when every path changed since is a Markdown file under docs/ or memory-global/ that
the check neither declares nor names in its command. Anything else, and any doubt (a
missing tree object, a git failure, a record without a tree sha), is a re-run whose
verify_final_check event names the failed condition.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from argparse import Namespace
from dataclasses import asdict

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult
from agentctl.plan import PlanError, parse_plan
from agentctl.state import (
    Actor,
    Criterion,
    CriterionType,
    FinalCheck,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


class _LazyCheckCache:
    """agentctl.check_cache, imported on first use, so that against a tree without the
    narrowed lookup each test fails on its own assertion instead of at collection."""

    def __getattr__(self, name):
        return getattr(importlib.import_module("agentctl.check_cache"), name)


check_cache = _LazyCheckCache()

A, B, C = "aaaa1111", "bbbb2222", "cccc3333"
CMD = "pytest -q tests/unit"
REFUSED = "narrow_refused:"


def ns(**kw):
    return Namespace(**kw)


def _tree(head):
    return f"tree-of-{head}"


class _Runner:
    """Git probes plus bash checks. `diffs` maps (old tree, new tree) to the changed
    paths `git diff-tree` would print, or to an int exit code for a failing git call;
    an unlisted pair fails. `missing` lists tree objects `git cat-file -e` rejects."""

    def __init__(self, head, diffs=None, missing=(), exits=None):
        self.head = head
        self.diffs = diffs or {}
        self.missing = set(missing)
        self.exits = exits or {}
        self.bash_calls: list[str] = []

    def __call__(self, argv, *, timeout=None, stdin=""):
        git_argv = argv[argv.index("git"):] if argv[:1] == ["env"] and "git" in argv else argv
        if git_argv[:1] == ["git"]:
            if "rev-parse" in git_argv:
                if "--git-path" in git_argv:
                    return RunResult(0, stdout=".git/index", stderr="")
                return RunResult(0, stdout=self.head, stderr="")
            if "write-tree" in git_argv:
                return RunResult(0, stdout=_tree(self.head), stderr="")
            if "cat-file" in git_argv:
                return RunResult(1 if git_argv[-1].split("^")[0] in self.missing else 0, stdout="", stderr="")
            if "diff-tree" in git_argv:
                old, new = git_argv[-2:]
                spec = self.diffs.get((old, new), 128)
                if isinstance(spec, int):
                    return RunResult(spec, stdout="", stderr="fatal")
                return RunResult(0, stdout="".join(f"{p}\0" for p in spec), stderr="")
            return RunResult(0, stdout="", stderr="")
        if argv[:2] == ["bash", "-c"]:
            self.bash_calls.append(argv[2])
            code = next((c for sub, c in self.exits.items() if sub in argv[2]), 0)
            return RunResult(code, stdout="", stderr="")
        raise AssertionError(f"unexpected call: {argv}")


def _stage(index, command, reads=None, outcome=None):
    return Stage(
        index=index, title=f"s{index}",
        subject=Subject(material="m", result="tests pass"),
        means=Means(means="pytest", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(
            criterion_type=CriterionType.MEASURABLE.value, done_criterion="passes",
            verify_command=command, expected_exit=0, verify_reads=reads,
        ),
        outcome=outcome or Outcome(status=StageStatus.PASSED.value),
    )


def _verifying(store, sid, stages, final_checks=()):
    state = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.VERIFYING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=list(stages), final_check=list(final_checks),
    )
    store.save(state)
    return state


def _verify(store, sid, runner):
    return cli.cmd_verify_final(ns(session=sid), store=store, runner=runner)


def _again(store, sid):
    state = store.load(sid)
    state.node = Node.VERIFYING.value
    state.resolution = GateRecord("resolution", armed=False, passed=False)
    store.save(state)


def _last_run(store, sid):
    """{check id: (decision, reason)} of the latest verify-final."""
    history = store.load(sid).history
    end = max(i for i, e in enumerate(history) if e["event"] == "verify_final_run")
    found = {}
    for e in reversed(history[:end]):
        if e["event"] == "verify_final_run":
            break
        if e["event"] == "verify_final_check":
            found[e["check"]] = (e["decision"], e["reason"])
    return found


def _two_runs(store, sid, stages, final_checks=(), *, diffs=None, missing=(), second_head=B):
    """verify-final on tree A (everything runs and is cached), then on a moved tree."""
    _verifying(store, sid, stages, final_checks)
    first = _Runner(A)
    _verify(store, sid, first)
    _again(store, sid)
    second = _Runner(second_head, diffs=diffs, missing=missing)
    _verify(store, sid, second)
    return first, second


def _docs_change(*paths):
    return {(_tree(A), _tree(B)): list(paths)}


# --- the narrowed hit -----------------------------------------------------------------

def test_docs_only_change_outside_the_reads_is_served_narrowed(store):
    _, second = _two_runs(
        store, "n-hit", [_stage(1, CMD, reads=["docs/guide.md"])],
        diffs=_docs_change("docs/notes/extra.md"),
    )
    assert second.bash_calls == []
    decision, reason = _last_run(store, "n-hit")["stage:1"]
    assert decision == "narrowed"
    assert reason.startswith("narrowed")


def test_second_verify_final_on_the_same_tree_is_a_plain_hit(store):
    _, third_before = _two_runs(
        store, "n-twice", [_stage(1, CMD, reads=[])], diffs=_docs_change("docs/a.md"),
    )
    _again(store, "n-twice")
    third = _Runner(B)
    _verify(store, "n-twice", third)
    assert third.bash_calls == []
    assert _last_run(store, "n-twice")["stage:1"][0] == "hit"


def test_an_empty_declaration_still_narrows(store):
    _two_runs(store, "n-empty", [_stage(1, CMD, reads=[])], diffs=_docs_change("memory-global/leaves/x.md"))
    assert _last_run(store, "n-empty")["stage:1"][0] == "narrowed"


def test_same_tree_under_a_new_head_is_narrowed(store):
    _two_runs(store, "n-same-tree", [_stage(1, CMD, reads=[])], diffs=_docs_change())
    assert _last_run(store, "n-same-tree")["stage:1"][0] == "narrowed"


def test_cacheable_final_check_is_narrowed_too(store):
    final = FinalCheck(command="python3 scripts/check.py", cacheable=True, verify_reads=["docs/a.md"])
    _, second = _two_runs(
        store, "n-final", [_stage(1, CMD, reads=[])], [final], diffs=_docs_change("docs/b.md"),
    )
    assert second.bash_calls == []
    run = _last_run(store, "n-final")
    assert run["stage:1"][0] == "narrowed"
    assert run["final_check:1"][0] == "narrowed"


def test_non_cacheable_final_check_reruns_with_its_stage_two_reason(store):
    final = FinalCheck(command="python3 scripts/check.py", verify_reads=["docs/a.md"])
    _, second = _two_runs(
        store, "n-final-nc", [_stage(1, CMD, reads=[])], [final], diffs=_docs_change("docs/b.md"),
    )
    assert _last_run(store, "n-final-nc")["final_check:1"] == ("miss", "not_cacheable")
    assert second.bash_calls == ["python3 scripts/check.py"]


def test_stage_outcome_green_is_a_narrowing_base(store):
    outcome = Outcome(
        status=StageStatus.PASSED.value, checked_tree_ok=True, checked_tree_sha=_tree(A),
        checked_tree_identity=_identity_for(A, CMD),
    )
    _verifying(store, "n-outcome", [_stage(1, CMD, reads=[], outcome=outcome)])
    runner = _Runner(B, diffs=_docs_change("docs/a.md"))
    _verify(store, "n-outcome", runner)
    assert runner.bash_calls == []
    assert _last_run(store, "n-outcome")["stage:1"][0] == "narrowed"


def _identity_for(head, command):
    """The checked_tree_identity record-result stores for a fake runner at `head`."""
    tree_id = hashlib.sha256(head.encode() + b"\x00" + _tree(head).encode()).hexdigest()
    definition = hashlib.sha256()
    for part in (command, "0", "", ""):
        definition.update(part.encode())
        definition.update(b"\x00")
    return f"{tree_id}:{definition.hexdigest()}"


# --- re-runs, each naming its failed condition -----------------------------------------

def test_no_declaration_keeps_the_stage_two_reason(store):
    _, second = _two_runs(store, "n-none", [_stage(1, CMD)], diffs=_docs_change("docs/a.md"))
    assert _last_run(store, "n-none")["stage:1"] == ("miss", "no_cached_green")
    assert second.bash_calls == [CMD]


@pytest.mark.parametrize("changed", [
    ["scripts/agentctl/cli.py"],
    ["tests/unit/test_a.py"],
    ["config.md"],
    ["CLAUDE.md"],
    ["skills/specializations/planner/policy.md"],
    ["README.md"],
    ["docs/data.json"],
    ["docsx/a.md"],
    ["docs/a.md", "scripts/x.py"],
    ["docs/moved.md", "scripts/moved.md"],
], ids=lambda c: "+".join(c))
def test_a_path_outside_the_allowlist_reruns(store, changed):
    _, second = _two_runs(store, "n-out", [_stage(1, CMD, reads=[])], diffs=_docs_change(*changed))
    assert _last_run(store, "n-out")["stage:1"] == ("miss", REFUSED + "path_outside_allowlist")
    assert second.bash_calls == [CMD]


@pytest.mark.parametrize("reads, changed", [
    (["docs/guide.md"], "docs/guide.md"),
    (["docs/*.md"], "docs/guide.md"),
    (["docs/**/*.md"], "docs/deep/er/guide.md"),
    (["memory-global/leaves/[ab].md"], "memory-global/leaves/c.md"),
])
def test_a_declared_path_reruns(store, reads, changed):
    _two_runs(store, "n-decl", [_stage(1, CMD, reads=reads)], diffs=_docs_change(changed))
    assert _last_run(store, "n-decl")["stage:1"] == ("miss", REFUSED + "path_in_verify_reads")


@pytest.mark.parametrize("command, changed", [
    ("cat docs/guide.md", "docs/guide.md"),
    ("grep -r needle docs", "docs/a/b.md"),
    ("grep -r needle docs/", "docs/a/b.md"),
    ("ls ./memory-global/leaves", "memory-global/leaves/x.md"),
    ("python3 check.py --root=docs/a", "docs/a/b.md"),
    ("cat 'docs/a b.md'", "docs/a b.md"),
])
def test_a_path_named_in_the_command_reruns(store, command, changed):
    _two_runs(store, "n-named", [_stage(1, command, reads=[])], diffs=_docs_change(changed))
    assert _last_run(store, "n-named")["stage:1"] == ("miss", REFUSED + "path_named_in_command")


def test_a_missing_tree_object_reruns(store):
    _, second = _two_runs(
        store, "n-missing", [_stage(1, CMD, reads=[])], diffs=_docs_change("docs/a.md"),
        missing=[_tree(A)],
    )
    assert _last_run(store, "n-missing")["stage:1"] == ("miss", REFUSED + "tree_object_missing")
    assert second.bash_calls == [CMD]


@pytest.mark.parametrize("diffs", [{(_tree(A), _tree(B)): 128}, {}])
def test_a_git_failure_reruns(store, diffs):
    _, second = _two_runs(store, "n-git", [_stage(1, CMD, reads=[])], diffs=diffs)
    assert _last_run(store, "n-git")["stage:1"] == ("miss", REFUSED + "git_failed")
    assert second.bash_calls == [CMD]


def test_records_without_a_tree_sha_are_no_base(store):
    _verifying(store, "n-old", [_stage(1, CMD, reads=[])])
    _verify(store, "n-old", _Runner(A))
    state = store.load("n-old")
    for entry in state.check_cache.values():
        entry.pop("tree", None)
    store.save(state)
    _again(store, "n-old")
    second = _Runner(B, diffs=_docs_change("docs/a.md"))
    _verify(store, "n-old", second)
    assert _last_run(store, "n-old")["stage:1"] == ("miss", REFUSED + "no_candidate")
    assert second.bash_calls == [CMD]


def test_a_red_run_is_never_a_base(store):
    _verifying(store, "n-red", [_stage(1, CMD, reads=[])])
    _verify(store, "n-red", _Runner(A, exits={CMD: 1}))
    _again(store, "n-red")
    second = _Runner(B, diffs=_docs_change("docs/a.md"))
    _verify(store, "n-red", second)
    assert _last_run(store, "n-red")["stage:1"] == ("miss", REFUSED + "no_candidate")


def test_a_changed_command_has_no_base(store):
    state = _verifying(store, "n-cmd", [_stage(1, CMD, reads=[])])
    _verify(store, "n-cmd", _Runner(A))
    state = store.load("n-cmd")
    state.stages[0].criterion.verify_command = CMD + " -x"
    store.save(state)
    _again(store, "n-cmd")
    _verify(store, "n-cmd", _Runner(B, diffs=_docs_change("docs/a.md")))
    assert _last_run(store, "n-cmd")["stage:1"] == ("miss", REFUSED + "no_candidate")


def test_a_narrowed_green_is_not_itself_a_base(store):
    _two_runs(store, "n-chain", [_stage(1, CMD, reads=[])], diffs=_docs_change("docs/a.md"))
    assert _last_run(store, "n-chain")["stage:1"][0] == "narrowed"
    _again(store, "n-chain")
    third = _Runner(C, diffs={(_tree(B), _tree(C)): ["docs/b.md"]})
    _verify(store, "n-chain", third)
    assert _last_run(store, "n-chain")["stage:1"] == ("miss", REFUSED + "git_failed")
    assert third.bash_calls == [CMD]


# --- the pure helpers -------------------------------------------------------------------

@pytest.mark.parametrize("path, expected", [
    ("docs/a.md", True),
    ("docs/x/y/z.md", True),
    ("memory-global/leaves/a.md", True),
    ("memory-global/a.md", True),
    ("docs/a.txt", False),
    ("docs/a.MD", False),
    ("docsx/a.md", False),
    ("a/docs/b.md", False),
    ("/docs/a.md", False),
    ("docs/../scripts/a.md", False),
    ("docs/", False),
    ("README.md", False),
    ("CLAUDE.md", False),
    ("skills/a/SKILL.md", False),
    ("memory/a.md", False),
])
def test_allowlist_is_closed(path, expected):
    assert check_cache.allowlisted(path) is expected


@pytest.mark.parametrize("path, patterns, expected", [
    ("docs/a.md", ["docs/*.md"], True),
    ("docs/x/a.md", ["docs/*.md"], False),
    ("docs/x/a.md", ["docs/**/*.md"], True),
    ("docs/a.md", ["docs/**/*.md"], True),
    ("docs/a.md", ["docs/a.md"], True),
    ("docs/a.md", ["docs/b.md"], False),
    ("docs/a.md", ["docs/[ab].md"], True),
    ("docs/a.md", ["docs/{a,b}.md"], True),
    ("docs/a.md", [""], True),
    ("docs/a.md", [], False),
    ("docs/a.md", ["docs/?.md"], True),
    ("memory-global/leaves/a.md", ["docs/**"], False),
])
def test_reads_matching_over_approximates(path, patterns, expected):
    assert check_cache.reads_match(path, patterns) is expected


# --- the declaration in the plan --------------------------------------------------------

def _plan(stage_extra=None, final_extra=None):
    stage = {
        "index": 1, "title": "s", "executor": "in_thread", "expected_result_image": "i",
        "done_criterion": "d", "verify_command": "pytest tests/x.py", **(stage_extra or {}),
    }
    plan = {"meta": {"task_id": "t"}, "stage": [stage]}
    if final_extra is not None:
        plan["final_check"] = [{"command": "pytest tests/y.py", **final_extra}]
    return plan


def test_plan_loader_reads_the_declaration():
    doc = parse_plan(_plan({"verify_reads": ["docs/a.md"]}, {"verify_reads": []}))
    assert doc.stages[0].criterion.verify_reads == ["docs/a.md"]
    assert doc.meta.final_check[0].verify_reads == []


def test_plan_loader_defaults_to_no_declaration():
    doc = parse_plan(_plan(final_extra={}))
    assert doc.stages[0].criterion.verify_reads is None
    assert doc.meta.final_check[0].verify_reads is None


@pytest.mark.parametrize("bad", ["docs/a.md", [1], [""], ["/etc/x"], ["../x"], ["docs/../x"], None])
def test_plan_loader_rejects_a_malformed_declaration(bad):
    with pytest.raises(PlanError, match="verify_reads"):
        parse_plan(_plan({"verify_reads": bad}))
    with pytest.raises(PlanError, match="verify_reads"):
        parse_plan(_plan(final_extra={"verify_reads": bad}))


def test_declaration_moves_the_stage_and_final_check_digests():
    from agentctl.plan import order_extra_digest
    from agentctl.stage_norm import StageNorm

    plain = parse_plan(_plan(final_extra={}))
    reads = parse_plan(_plan({"verify_reads": ["docs/a.md"]}, {"verify_reads": ["docs/a.md"]}))
    empty = parse_plan(_plan({"verify_reads": []}, {"verify_reads": []}))
    carry = [StageNorm.from_stage(d.stages[0]).carry_digest() for d in (plain, reads, empty)]
    review = [StageNorm.from_stage(d.stages[0]).review_digest() for d in (plain, reads, empty)]
    extra = [order_extra_digest(d.meta) for d in (plain, reads, empty)]
    for digests in (carry, review, extra):
        assert len(set(digests)) == 3


def test_an_undeclared_check_leaves_the_stage_keys_untouched():
    from agentctl.stage_norm import StageNorm

    norm = StageNorm.from_stage(parse_plan(_plan()).stages[0])
    assert "verify_reads" not in repr(norm.carry_key())
    assert "verify_reads" not in norm._review_payload(indexed=True)


def test_declaration_is_rendered():
    from agentctl.render import render_plan_md

    text = render_plan_md(parse_plan(_plan({"verify_reads": ["docs/a.md"]}, {"verify_reads": []})))
    assert "docs/a.md" in text
    assert "verify_reads" in text or "reads" in text


def test_declaration_survives_the_state_round_trip(store):
    final = FinalCheck(command="x", verify_reads=[])
    _verifying(store, "n-rt", [_stage(1, CMD, reads=[])], [final])
    state = store.load("n-rt")
    assert state.stages[0].criterion.verify_reads == []
    assert state.final_check[0].verify_reads == []
    assert json.loads(json.dumps(asdict(state.stages[0].criterion)))["verify_reads"] == []
