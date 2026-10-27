"""Replay of the denial fixture (`fixtures/resource_grant_replay/denials.jsonl`) through the
resource-grant PreToolUse hook's entry point (`hook.run`).

Each case's approved set is built the way a spawn builds it: the stage's resources from
`plan_resources.resource_set_from_entries` over the case's grants and the commit/push resources
`grants.derive_stage_grants` derives for the case's branch (a temporary git repository on that
branch), the workdir baseline when the kind runs under acceptEdits, and the child's real
`permissions.allow` from `build_child_settings`. The opaque-code judge is a stub that answers
with the case's `expected_effects`; no `claude` process is started.

The answer key says what the *plan* sanctions; the hook is stricter than that where the resolver
or the opaque-code reader cannot name an effect, so a few cases are expected to fall through to
the harness (decision `none`) instead of `allow`/`deny`. Those are listed in `FALL_THROUGH`, each
with its reason, and the test pins them: a case that starts to resolve must be moved out of the
list on purpose.

To see the per-case table and the per-class summary:
    python3 -m pytest scripts/tests/test_resource_grant_replay.py -q -s
"""
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

import pytest

import resource_grant_hook_support as support
import test_tool_contracts_segments as seg_tests
from agentctl import segment_resolver
from agentctl.dispatch import RunResult

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "resource_grant_replay" / "denials.jsonl"
CASES = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]

#: case -> why the hook makes no decision although the answer key expects allow/deny
FALL_THROUGH = {
    "prefix-mismatch-02": "`git push origin x:x` is not resolved by the per-segment resolver",
    "prefix-mismatch-03": "`git push origin x:x` is not resolved by the per-segment resolver",
    "prefix-mismatch-04": "`git push origin x:x` is not resolved by the per-segment resolver",
    "python-c-01": "the code imports pytest, outside the opaque-code reader's allow-list: undeterminable",
    "python-c-02": "the code imports inspect, outside the opaque-code reader's allow-list: undeterminable",
    "uncovered-write-07": "`git -C <other repository> commit` is not resolved by the per-segment resolver",
    "python-c-03": "opaque code that writes nothing: the judge names no reads, so its reads are not confined",
    "python-stdin-03": "opaque code that writes nothing: the judge names no reads, so its reads are not confined",
    "uncovered-write-01": "an allow entry (`git -C <venue> rev-parse HEAD:*`) matches the call leniently: the redirect is ignored",
    "uncovered-write-02": "a multi-line call: the lenient reading of any `Bash(...)` entry covers an unresolved newline",
    "uncovered-write-03": "a multi-line call: the lenient reading of any `Bash(...)` entry covers an unresolved newline",
}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    monkeypatch.delenv("CDPATH", raising=False)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "AGENTCTL_RESOURCE_HOOK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def _patient_git_config_probe(monkeypatch):
    """The resolver's `git config` probes time out after 5 s; on a machine loaded by a parallel run a timeout
    turns a resolvable segment into an unresolved one, so the replay waits for them."""
    monkeypatch.setattr(segment_resolver, "_GIT_CONFIG_TIMEOUT_S", 120)


@pytest.fixture(scope="module")
def hook():
    return support.load_hook()


@pytest.fixture(scope="module")
def spawn():
    return support.load_spawn()


@pytest.fixture(scope="module")
def _shared_world(tmp_path_factory):
    world = seg_tests.build_world(tmp_path_factory.mktemp("replay"))
    scratchpad = world["tmp"] / support.PARENT_SESSION / "scratchpad"
    scratchpad.mkdir(parents=True)
    world["scratch"] = scratchpad
    return world


def reset_world(world):
    """Back to the shared venue's starting branch, plus the local branch `uncovered-write-06` pushes (the
    developer owned it in the session that case came from); `_checkout_branch` renames the current branch."""
    venue = world["venue"]
    seg_tests._git(venue, "checkout", "-q", "-B", "feature")
    seg_tests._git(venue, "branch", "-f", "plan-dashboard", "feature")


@pytest.fixture
def world(_shared_world):
    return _shared_world


class JudgeStub:
    """The opaque-code judge's runner: answers with the case's expected effects, never a real model."""

    def __init__(self, effects):
        self.effects = effects
        self.calls = 0

    def __call__(self, argv, *, timeout, stdin=""):
        self.calls += 1
        reply = json.dumps({"effects": self.effects, "undeterminable": False, "reason": "replay"})
        return RunResult(0, reply, "", timed_out=False)


def _effects(case, world):
    return [json.loads(seg_tests._subst(json.dumps(e), world)) for e in case["expected_effects"]]


def case_allow_list(case, rules):
    """The `permissions.allow` the case's era gave the child: its own grants plus, for a case the
    harness allowed by an ambient entry, that entry (the source line quotes it)."""
    ambient = re.findall(r"entry (Bash\(.*?\)) in", case["source"]) if case.get("allowed_by") == "allow-entry" else []
    return [*rules, *ambient]


def replay(case, world, tmp_path, hook, spawn, *, today=False):
    """`today=True` gives the child the allow list `build_child_settings` yields now (kind baselines
    included) instead of the case's era list."""
    reset_world(world)
    seg_tests._checkout_branch(world, case)
    rules = [seg_tests._subst(g, world) for g in case["grants"]]
    child = support.link_child(
        spawn, tmp_path / "state", world["venue"], kind=case["kind"], branch=case["branch"], rules=rules,
        allow=None if today else case_allow_list(case, rules))
    if case["tool"] == "Bash":
        payload = child.payload("Bash", command=seg_tests._subst(case["command"], world))
    else:
        payload = child.payload(case["tool"], file_path=seg_tests._subst(case["path"], world),
                                content="x")
    judge = JudgeStub(_effects(case, world))
    answer = hook.run(payload, child.env, runner=judge, cache_dir=tmp_path / "judge-cache")
    decision = json.loads(answer)["hookSpecificOutput"]["permissionDecision"] if answer else "none"
    row = child.log_rows()[-1]
    assert row["decision"] == decision
    return decision, row, answer


def expected_decision(case):
    return "none" if case["id"] in FALL_THROUGH else case["expect"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_case_through_the_hook(case, world, tmp_path, hook, spawn):
    decision, row, answer = replay(case, world, tmp_path, hook, spawn)
    assert decision == expected_decision(case), (case["id"], row)
    if case.get("allowed_by"):
        assert decision != "deny", "a call the harness already allows must never be denied"
    if decision == "deny":
        assert "PERMISSION-REQUEST:" in answer and "Resource:" in answer and "Rule:" in answer


def test_fall_through_list_names_only_fixture_cases():
    assert set(FALL_THROUGH) <= {case["id"] for case in CASES}


def test_every_class_is_summarised(world, tmp_path_factory, hook, spawn, capsys):
    table = defaultdict(Counter)
    lines = []
    for number, case in enumerate(CASES):
        tmp = tmp_path_factory.mktemp(f"summary{number}")
        decision, row, _ = replay(case, world, tmp, hook, spawn)
        assert decision == expected_decision(case), (case["id"], row)
        table[case["class"]][decision] += 1
        lines.append(f"{case['id']:<24} {case['tool']:<6} expect={case['expect']:<5} "
                     f"actual={decision:<5} reason_class={row['reason_class']}")
    with capsys.disabled():
        print("\n" + "\n".join(lines))
        print("\nper class (allow/deny/none):")
        for klass, counts in sorted(table.items()):
            print(f"  {klass:<20} {counts['allow']}/{counts['deny']}/{counts['none']}")
    for klass in ("uncovered-write",):
        assert table[klass]["deny"] == sum(1 for c in CASES if c["class"] == klass and c["expect"] == "deny"
                                           and c["id"] not in FALL_THROUGH) >= 3
    harness_allowed = [c for c in CASES if c.get("allowed_by")]
    assert harness_allowed and all(
        replay(c, world, tmp_path_factory.mktemp("allowed"), hook, spawn)[0] != "deny" for c in harness_allowed)


@pytest.mark.parametrize("case", [c for c in CASES if c["expect"] != "deny"], ids=lambda c: c["id"])
def test_no_sanctioned_case_is_denied_under_todays_allow_list(case, world, tmp_path, hook, spawn):
    decision, row, _ = replay(case, world, tmp_path, hook, spawn, today=True)
    assert decision != "deny", row
