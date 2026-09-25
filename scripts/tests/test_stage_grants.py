"""Coverage for grants.py's validator/coverage-check surface and the cli.py
grant-reporting/runtime-grant/PERMISSION-REQUEST-classification commands, per
stage 1's plan procedure step 6 ("Wrong if" scenarios).

Four areas, in order: (1) validate_rule/validate_add_dir accept/refuse
tables, (2) grant_covers_call's Bash and path-tool coverage tables, (3)
StageGrants.effective_tuple()'s order-independence (the projection
diff_plans' growth check relies on), (4) the cli.py integration surface --
cmd_stage_grants/cmd_grant_stats/cmd_resolve_permission --scope stage, and
cmd_dispatch's PERMISSION-REQUEST branch routing a covered denial to a
materialization defect vs an uncovered one to a genuine user ask.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from argparse import Namespace

from agentctl import cli
from agentctl import plan as plan_mod
from agentctl.dispatch import RunResult
from agentctl.grants import (
    AddDirGrant,
    GrantValidationError,
    RuleGrant,
    StageGrants,
    grant_covers_call,
    validate_add_dir,
    validate_rule,
)
from lib import config_root, widening_targets
from conftest import STAGE_OBSERVATIONS


def ns(**kw):
    return Namespace(**kw)


# --- (1) validate_rule accept/refuse table ---------------------------------

_ACCEPTED_RULES = [
    "Bash(git status:*)",
    "Bash(git status)",
    "Bash(python3 scripts/tests/test_foo.py:*)",
    "Bash(python3 -m pytest scripts/tests -q:*)",
    # `-m <module>` naming an ordinary (non-launcher) module is accepted --
    # only a module itself capable of launching arbitrary code is refused
    # (finding B1).
    "Bash(python3 -m json.tool data.json:*)",
    # A redirect-merge form (`2>&1`) must not be mistaken for a top-level
    # `&`-separator and split into a bogus 2-segment compound command
    # (finding S1).
    "Bash(pytest -q 2>&1:*)",
    "Edit(//home/user/repo/scripts/foo.py)",
    "Read(//home/user/repo/README.md)",
    "WebFetch(domain:code.claude.com)",
]

_REFUSED_RULES = [
    "",
    "not-a-rule-shape",
    "Bash()",
    "Bash( :*)",
    "Bash(claude --print hi:*)",
    # finding B1: a wrapper token stripped before judging the program --
    # a wrapped `claude` invocation is refused via the same check as the
    # bare form above, now that sudo/doas/xargs/eval/time/nice/stdbuf are
    # wrapper tokens too.
    "Bash(sudo claude --print hi:*)",
    "Bash(stdbuf -o0 claude:*)",
    "Bash(nice -n 5 claude:*)",
    "Bash(sudo -u x claude:*)",
    "Bash(setsid claude:*)",
    "Bash(doas claude --print hi:*)",
    "Bash(time claude --print hi:*)",
    "Bash(nice claude --print hi:*)",
    "Bash(stdbuf claude --print hi:*)",
    # `agentctl_user_authority_call` only recognizes `python -m agentctl <verb>`
    # or a path literally ending `agentctl-cli.py <verb>` -- not the module
    # file `scripts/agentctl/cli.py` (a different, non-recognized spelling).
    "Bash(python3 -m agentctl resolve --by user:*)",
    "Bash(scripts/apply-settings.sh:*)",
    "Bash(bash scripts/install-reminder-hooks.sh:*)",
    "Bash(sh scripts/install-reminder-hooks.sh:*)",
    "Bash(crontab -e:*)",
    "Bash(python3:*)",
    "Bash(bash:*)",
    # Bare `env` with no operand -- a wrapper token that consumes the whole
    # command, leaving nothing to validate; must be refused outright rather
    # than silently accepted (would otherwise cover any `env <anything>`).
    "Bash(env:*)",
    "Bash(timeout 30:*)",
    "Bash(tee ~/.claude/settings.json:*)",
    "Bash(cp foo.txt ~/.claude-agent/state/x:*)",
    "Edit(//home/the0/.claude/settings.json)",
    f"Edit(//{config_root.agentctl_state_dir()}/session.json)",
    # finding #2: bare `*` command matches anything at materialization time.
    "Bash(*)",
    # finding #2: a trailing bare `-c`/`-e`/`-m` names no inline code/module
    # text -- the wildcard would let the child supply arbitrary text the rule
    # never pinned.
    "Bash(python3 -c:*)",
    "Bash(node -e:*)",
    "Bash(python3 -m:*)",
    # finding #2: eval/sudo/xargs with no operand at all name no concrete
    # downstream command for the validator to have checked.
    "Bash(eval:*)",
    "Bash(sudo:*)",
    "Bash(xargs:*)",
    # finding B1 (interpreter allowlist): a `-c`/`-e` flag WITH an argument is
    # refused just as readily as the bare trailing form above -- the flag
    # itself is the danger, not merely its absence of an operand. A `;`
    # inside the single-quoted argument is inert to the shell and must not
    # be mistaken for a real top-level separator -- also the regression pin
    # for the quote-unaware segmentation bug DR-V's derivation hit (this
    # rule used to be in _ACCEPTED_RULES before the allowlist rework).
    "Bash(python -c 'import mod; assert True':*)",
    "Bash(python3 -c 'import os; os.system(1)':*)",
    # finding B1: `-m <module>` naming a module that is ITSELF a launcher
    # (runpy/code/pip/...) is refused -- it turns the interpreter into an
    # unbounded shell/REPL-equivalent even though the rule text names a
    # "module", not raw inline code.
    "Bash(python3 -m runpy foo.py:*)",
    "Bash(python3 -m pip install x:*)",
    "Bash(python3 -m code:*)",
    # finding B1: `awk`/`find ... -exec` are refused unconditionally --
    # each is a DSL (or a find-clause) with an unbounded command-execution
    # primitive, regardless of the specific arguments named.
    "Bash(awk '{print}' file:*)",
    "Bash(find . -exec rm {} \\;:*)",
    # finding #2: a wildcarded agentctl invocation naming no verb covers every
    # verb at materialization time, including a user-authority one.
    "Bash(python3 -m agentctl:*)",
    "Bash(agentctl-cli.py:*)",
    # finding #5 (second half): ANY `:*` wildcard on a write-capable program
    # is refused outright, even when the declared argument is not itself a
    # G-target -- the wildcard admits extra args at materialization time the
    # rule text never named.
    "Bash(tee /tmp/foo.txt:*)",
    "Bash(cp foo.txt /tmp/bar.txt:*)",
    # finding #3: an empty non-Bash path must be refused, not silently
    # accepted.
    "Edit()",
    "Edit(//)",
    # finding #3: a glob path can expand to match an unbounded/unpredictable
    # set of real paths, including a protected root -- refused outright.
    "Edit(//**)",
    "Edit(**)",
    "Edit(//home/the0/**)",
    # finding B2: `?` (single-char wildcard) and `[...]` (character class)
    # are glob metacharacters too, not only `*` -- each can expand to match
    # an unpredictable set of real paths at materialization time.
    "Edit(//home/user/repo/scripts/foo?.py)",
    "Edit(//home/user/repo/scripts/fo[o].py)",
    "Edit(//home/user/repo/scripts/{foo,bar}.py)",
    # A `..` segment must not carry a rule past the protected-target check:
    # the harness form `//abs` decodes to `/abs` before normalization.
    f"Edit(//{str(config_root.agentctl_state_dir()).lstrip('/')}/../state/s.json)",
    f"Edit(//{str(config_root.harness_config_root()).lstrip('/')}/x/../settings.json)",
    # finding N3: a path under a `.git` directory (git hooks are an
    # executable, supply-chain-relevant surface) is refused, matching the
    # same exclusion `_resolve_for_match` already applies on the coverage
    # side.
    "Edit(//home/user/repo/.git/hooks/pre-commit)",
    # finding B3: an output redirect (`>`/`>>`) in a declared Bash rule
    # whose destination is a G-target is refused, even when the invoked
    # program itself is not one of the write-capable programs checked
    # above.
    "Bash(echo hi > ~/.claude/settings.json:*)",
    f"Bash(echo hi >> {config_root.agentctl_state_dir()}/session.json:*)",
]


@pytest.mark.parametrize("rule", _ACCEPTED_RULES)
def test_validate_rule_accepts(rule):
    validate_rule(rule)  # must not raise


@pytest.mark.parametrize("rule", _REFUSED_RULES)
def test_validate_rule_refuses(rule):
    with pytest.raises(GrantValidationError):
        validate_rule(rule)


def test_validate_rule_refuses_unlexable_command():
    with pytest.raises(GrantValidationError):
        validate_rule("Bash(echo 'unterminated:*)")


# --- validate_add_dir table -------------------------------------------------


def test_validate_add_dir_accepts_ordinary_path():
    validate_add_dir("/home/user/repo/scripts", "read")


def _refuse_add_dir_cases() -> list[tuple[str, str]]:
    # Derived from the live config_root resolution rather than a hardcoded
    # `~/.claude` literal -- on a migrated machine (CLAUDE_CONFIG_DIR set to
    # an isolated root) `~/.claude` may not be a protected root at all, so a
    # hardcoded guess silently stops exercising the refusal it names.
    agent_home = str(config_root.agent_home())
    harness_root = str(config_root.harness_config_root())
    state_dir = str(config_root.agentctl_state_dir())
    return [
        ("", "read"),
        (agent_home, "read"),
        (harness_root, "write"),
        (f"{state_dir}", "write"),
        ("/", "write"),  # ancestor of every protected root
        ("/home/user/repo", "delete"),  # unknown mode, refused regardless of path
    ]


@pytest.mark.parametrize("path,mode", _refuse_add_dir_cases())
def test_validate_add_dir_refuses(path, mode):
    with pytest.raises(GrantValidationError):
        validate_add_dir(path, mode)


# --- (2) grant_covers_call: Bash coverage table -----------------------------


def _grants_with_bash(rule: str) -> StageGrants:
    return StageGrants(allow=[RuleGrant(rule=rule, provenance="declared")])


def test_bash_wildcard_rule_covers_exact_and_with_args():
    grants = _grants_with_bash("Bash(python3 mod.py:*)")
    assert grant_covers_call(grants, "Bash", {"command": "python3 mod.py"})
    assert grant_covers_call(grants, "Bash", {"command": "python3 mod.py -q"})


def test_bash_wildcard_rule_does_not_cover_unrelated_prefix_collision():
    grants = _grants_with_bash("Bash(python3 mod.py:*)")
    assert not grant_covers_call(grants, "Bash", {"command": "python3 mod.pyx"})


def test_bash_exact_rule_requires_exact_match():
    grants = _grants_with_bash("Bash(git status)")
    assert grant_covers_call(grants, "Bash", {"command": "git status"})
    assert not grant_covers_call(grants, "Bash", {"command": "git status -s"})


def test_bash_compound_command_needs_every_segment_covered():
    grants = StageGrants(allow=[
        RuleGrant(rule="Bash(git add foo:*)", provenance="declared"),
        RuleGrant(rule="Bash(git commit -m x:*)", provenance="declared"),
    ])
    assert grant_covers_call(grants, "Bash", {"command": "git add foo && git commit -m x"})
    assert not grant_covers_call(
        grants, "Bash", {"command": "git add foo && git push origin main"}
    )


def test_bash_multiline_command_splits_into_segments():
    grants = StageGrants(allow=[
        RuleGrant(rule="Bash(black --check .:*)", provenance="declared"),
        RuleGrant(rule="Bash(ruff check .:*)", provenance="declared"),
    ])
    assert grant_covers_call(grants, "Bash", {"command": "black --check .\nruff check ."})


def test_bash_unresolvable_segment_never_covered_even_if_rule_matches_text():
    grants = _grants_with_bash("Bash(echo $FOO:*)")
    assert not grant_covers_call(grants, "Bash", {"command": "echo $FOO"})


def test_bash_bare_redirect_segment_not_covered_under_a_matching_prefix_grant():
    """A prefix grant naming the command's own literal text (`echo hi`) does
    not extend to a `>` redirect appended onto that same text -- the
    redirect's target is not something the grant text ever named, so the
    call is a planning miss (uncovered), never a materialization defect."""
    grants = _grants_with_bash("Bash(echo hi:*)")
    assert not grant_covers_call(grants, "Bash", {"command": "echo hi > /tmp/out"})


def test_bash_command_substitution_segment_not_covered_under_a_matching_prefix_grant():
    """Same rule, for a `$(...)` command-substitution segment: `Bash(echo:*)`
    textually prefix-matches `echo $(whoami)`, but the substitution's actual
    argument cannot be read off the rule text, so the call stays uncovered."""
    grants = _grants_with_bash("Bash(echo:*)")
    assert not grant_covers_call(grants, "Bash", {"command": "echo $(whoami)"})


def test_bash_empty_or_missing_command_not_covered():
    grants = _grants_with_bash("Bash(git status:*)")
    assert not grant_covers_call(grants, "Bash", {"command": ""})
    assert not grant_covers_call(grants, "Bash", {})


# --- grant_covers_call: path-tool coverage table ----------------------------


def test_edit_rule_covers_exact_resolved_path(tmp_path):
    target = tmp_path / "foo.py"
    target.write_text("x")
    grants = StageGrants(allow=[RuleGrant(rule=f"Edit(//{target})", provenance="declared")])
    assert grant_covers_call(grants, "Edit", {"file_path": str(target)})


def test_edit_rule_in_harness_double_slash_form_covers_absolute_path(tmp_path):
    """`Edit(//abs/path)` is the form DR-E emits and the harness matches; it
    must decode to `/abs/path`, not to the relative `abs/path`."""
    target = tmp_path / "foo.py"
    target.write_text("x")
    rule = f"Edit(//{str(target).lstrip('/')})"
    grants = StageGrants(allow=[RuleGrant(rule=rule, provenance="derived:DR-E")])
    assert grant_covers_call(grants, "Edit", {"file_path": str(target)})
    assert grant_covers_call(grants, "Write", {"file_path": str(target)})
    assert grant_covers_call(grants, "NotebookEdit", {"file_path": str(target)})


def test_project_relative_edit_rule_is_not_covered(tmp_path, monkeypatch):
    """A single-slash or bare rule path is project-relative; the child's
    project root is unknown to the check, so it fails toward not-covered."""
    target = tmp_path / "foo.py"
    target.write_text("x")
    monkeypatch.chdir(tmp_path)
    for rule in ("Edit(foo.py)", "Edit(/foo.py)"):
        grants = StageGrants(allow=[RuleGrant(rule=rule, provenance="declared")])
        assert not grant_covers_call(grants, "Edit", {"file_path": str(target)})


def test_edit_not_covered_by_unrelated_rule(tmp_path):
    target = tmp_path / "foo.py"
    other = tmp_path / "bar.py"
    target.write_text("x")
    other.write_text("y")
    grants = StageGrants(allow=[RuleGrant(rule=f"Edit(//{other})", provenance="declared")])
    assert not grant_covers_call(grants, "Edit", {"file_path": str(target)})


def test_write_tool_requires_write_mode_add_dir(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    target = sub / "foo.py"
    target.write_text("x")
    read_only = StageGrants(add_dirs=[AddDirGrant(path=str(sub), mode="read", provenance="declared")])
    assert not grant_covers_call(read_only, "Write", {"file_path": str(target)})
    writable = StageGrants(add_dirs=[AddDirGrant(path=str(sub), mode="write", provenance="declared")])
    assert grant_covers_call(writable, "Write", {"file_path": str(target)})


def test_read_tool_covered_by_read_mode_add_dir(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    target = sub / "notes.md"
    target.write_text("x")
    grants = StageGrants(add_dirs=[AddDirGrant(path=str(sub), mode="read", provenance="declared")])
    assert grant_covers_call(grants, "Read", {"file_path": str(target)})


def test_path_under_dot_git_or_dot_claude_never_matched(tmp_path):
    git_dir = tmp_path / ".git" / "hooks"
    git_dir.mkdir(parents=True)
    target = git_dir / "pre-commit"
    target.write_text("x")
    grants = StageGrants(add_dirs=[AddDirGrant(path=str(tmp_path), mode="write", provenance="declared")])
    assert not grant_covers_call(grants, "Write", {"file_path": str(target)})


def test_unknown_tool_name_never_covered():
    grants = StageGrants(allow=[RuleGrant(rule="Bash(git status:*)", provenance="declared")])
    assert not grant_covers_call(grants, "SomeOtherTool", {"file_path": "/x"})


def test_dot_claude_under_non_protected_venue_still_matched(tmp_path):
    # A project-local `.claude/agent-memory/` directory (not the harness's
    # own `~/.claude` or the agentctl state dir) is a legitimate write
    # target -- e.g. project memory -- and must still be matchable; only
    # the specific protected roots widening_targets names are excluded,
    # not any path with a `.claude` component in general.
    mem_dir = tmp_path / ".claude" / "agent-memory"
    mem_dir.mkdir(parents=True)
    target = mem_dir / "leaf.md"
    target.write_text("x")
    grants = StageGrants(add_dirs=[AddDirGrant(path=str(tmp_path), mode="write", provenance="declared")])
    assert grant_covers_call(grants, "Write", {"file_path": str(target)})


def test_edit_rule_covers_write_and_notebookedit_calls(tmp_path):
    # The harness treats Edit-family rules as covering every file-editing
    # tool, so an `Edit(...)` grant must cover a `Write`/`NotebookEdit`
    # call onto the same resolved path even though the rule's own tool
    # token literally says "Edit".
    target = tmp_path / "foo.py"
    target.write_text("x")
    grants = StageGrants(allow=[RuleGrant(rule=f"Edit(//{target})", provenance="declared")])
    assert grant_covers_call(grants, "Write", {"file_path": str(target)})
    assert grant_covers_call(grants, "NotebookEdit", {"file_path": str(target)})


def test_edit_rule_does_not_cover_read_call(tmp_path):
    # The Edit-covers-Write/NotebookEdit widening is scoped to the other
    # file-EDITING tools only -- an Edit rule is not a general "any tool"
    # grant, so a Read call onto the same path stays uncovered.
    target = tmp_path / "foo.py"
    target.write_text("x")
    grants = StageGrants(allow=[RuleGrant(rule=f"Edit(//{target})", provenance="declared")])
    assert not grant_covers_call(grants, "Read", {"file_path": str(target)})


# --- (3) effective_tuple() order-independence -------------------------------


def test_effective_tuple_order_independent():
    a = StageGrants(
        allow=[RuleGrant(rule="Bash(a:*)", provenance="declared"), RuleGrant(rule="Bash(b:*)", provenance="derived:DR-V")],
        add_dirs=[AddDirGrant(path="/x", mode="read", provenance="declared")],
    )
    b = StageGrants(
        allow=[RuleGrant(rule="Bash(b:*)", provenance="derived:DR-V"), RuleGrant(rule="Bash(a:*)", provenance="declared")],
        add_dirs=[AddDirGrant(path="/x", mode="read", provenance="declared")],
    )
    assert a.effective_tuple() == b.effective_tuple()


def test_effective_tuple_ignores_provenance_label_churn():
    a = StageGrants(allow=[RuleGrant(rule="Bash(a:*)", provenance="declared")])
    b = StageGrants(allow=[RuleGrant(rule="Bash(a:*)", provenance="derived:DR-V")])
    assert a.effective_tuple() == b.effective_tuple()


def test_effective_tuple_detects_a_real_addition():
    a = StageGrants(allow=[RuleGrant(rule="Bash(a:*)", provenance="declared")])
    b = StageGrants(allow=[
        RuleGrant(rule="Bash(a:*)", provenance="declared"),
        RuleGrant(rule="Bash(rm -rf /:*)", provenance="declared"),
    ])
    assert a.effective_tuple() != b.effective_tuple()


# --- (4) cli.py integration: cmd_stage_grants / cmd_grant_stats / resolve-permission


def _to_executing(store, sid, fixtures_dir, plan_path=None):
    plan = plan_path or str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    return cli.cmd_next_stage(ns(session=sid), store=store)


def _write_plan_with_grants_block(fixtures_dir, tmp_path, grants_toml: str, name: str) -> str:
    """`plan_two_stage.toml` with a `[stage.grants]` table added to stage 1 --
    the same base fixture every other integration test in this section already
    submits successfully, so the only variable under test is the declared
    grants block itself."""
    text = (fixtures_dir / "plan_two_stage.toml").read_text()
    text = text.replace(
        'output_artifacts = ["mod.py"]\n',
        'output_artifacts = ["mod.py"]\n\n' + grants_toml,
        1,
    )
    out = tmp_path / name
    out.write_text(text)
    return str(out)


def _write_declared_grants_plan(fixtures_dir, tmp_path) -> str:
    return _write_plan_with_grants_block(
        fixtures_dir, tmp_path,
        "[stage.grants]\nallow = [\"Bash(git status:*)\"]\n",
        "plan_two_stage_declared_grants.toml",
    )


def test_load_plan_refuses_declared_wildcard_interpreter_rule(fixtures_dir, tmp_path):
    """(#5) A declared grant that would be refused standalone by
    `validate_rule` must fail the WHOLE plan load with `PlanError` -- an
    invalid declared grant can never reach a spawned child's --settings
    just because it arrived via `[stage.grants]` instead of a direct
    `resolve-permission --rule` call."""
    plan_path = _write_plan_with_grants_block(
        fixtures_dir, tmp_path,
        "[stage.grants]\nallow = [\"Bash(python3:*)\"]\n",
        "plan_bad_declared_rule.toml",
    )
    with pytest.raises(plan_mod.PlanError):
        plan_mod.load_plan(plan_path)


def test_load_plan_refuses_declared_write_add_dir_on_home(fixtures_dir, tmp_path):
    """(#5) Same refusal, for a declared `add_dirs` entry naming a write grant
    on a protected root (the home dir) -- `validate_add_dir`'s refusal must
    also surface as a `PlanError` at load time, not silently pass through."""
    home = str(config_root.agent_home().parent)  # the home dir itself, an ancestor of every protected root
    grants_toml = (
        "[stage.grants]\n"
        f'add_dirs = [{{ path = "{home}", mode = "write" }}]\n'
    )
    plan_path = _write_plan_with_grants_block(
        fixtures_dir, tmp_path, grants_toml, "plan_bad_declared_add_dir.toml",
    )
    with pytest.raises(plan_mod.PlanError):
        plan_mod.load_plan(plan_path)


def test_diff_plans_classifies_declared_grants_only_change_as_substantive(fixtures_dir, tmp_path):
    """(#6a) Two plans that are otherwise identical, differing ONLY by a
    declared `[stage.grants]` block added to stage 1, must classify as
    "substantive" -- the grant-growth escalation in `diff_plans` must fire
    on its own even when nothing else about the plan's structural signature
    or prose changed."""
    base = plan_mod.load_plan(str(fixtures_dir / "plan_two_stage.toml"))
    changed_path = _write_declared_grants_plan(fixtures_dir, tmp_path)
    changed = plan_mod.load_plan(changed_path)
    assert plan_mod.diff_plans(base, changed) == "substantive"


def test_cmd_stage_grants_reports_declared_grant_through_full_submit_approve_flow(
    store, fixtures_dir, tmp_path,
):
    """(#2) A stage's declared `[stage.grants]` block must survive the real
    cmd_submit_plan -> cmd_approve -> cmd_stage_grants path (not just the pure
    loader) and come back with provenance "declared"."""
    sid = "stage-grants-declared-full-flow"
    plan_path = _write_declared_grants_plan(fixtures_dir, tmp_path)
    _to_executing(store, sid, fixtures_dir, plan_path=plan_path)
    directive = cli.cmd_stage_grants(ns(session=sid, stage=1, json=True), store=store)
    assert directive.ok
    entries = directive.data["grants"]
    declared = [e for e in entries if e.get("rule") == "Bash(git status:*)"]
    assert len(declared) == 1
    assert declared[0]["provenance"] == "declared"


def test_cmd_stage_grants_unaffected_by_live_plan_path_edit_after_approval(store, fixtures_dir, tmp_path):
    """(#7) `cmd_stage_grants` reads declared/derived entries from the
    hash-verified snapshot taken at approval time, never the live
    `state.plan_path` -- editing the live file in place after approval (the
    coordinator is free to do this, e.g. a REVISE-verdict plan-review edit
    still pending its own re-approval) must not change what `cmd_stage_grants`
    reports for the already-approved stage.

    Submits a private tmp COPY of the shared `plan_two_stage.toml` fixture
    (never the committed fixture path itself) -- `state.plan_path` aliases
    whatever path was submitted rather than copying it, so writing to it in
    place, as this test's whole point is to do, would otherwise permanently
    corrupt the fixture every other test in this module reads."""
    sid = "stage-grants-live-edit-after-approval"
    plan_path = tmp_path / "plan_two_stage_live_edit.toml"
    plan_path.write_text((fixtures_dir / "plan_two_stage.toml").read_text())
    _to_executing(store, sid, fixtures_dir, plan_path=str(plan_path))
    state = store.load(sid)
    before = cli.cmd_stage_grants(ns(session=sid, stage=1, json=True), store=store)
    assert before.ok

    live_text = Path(state.plan_path).read_text()
    live_text = live_text.replace(
        'output_artifacts = ["mod.py"]', 'output_artifacts = ["mod.py", "extra.py"]', 1,
    )
    Path(state.plan_path).write_text(live_text)

    after = cli.cmd_stage_grants(ns(session=sid, stage=1, json=True), store=store)
    assert after.ok
    assert after.data == before.data


def test_cmd_stage_grants_reports_derived_dr_o_for_stage_one(store, fixtures_dir):
    sid = "stage-grants-report"
    _to_executing(store, sid, fixtures_dir)
    directive = cli.cmd_stage_grants(ns(session=sid, stage=1, json=True), store=store)
    assert directive.ok
    # Flat list, not a {"declared": [...], "derived": [...]} nesting -- each
    # entry names its own kind via "provenance" instead.
    entries = directive.data["grants"]
    assert isinstance(entries, list)
    derived_rules = {
        e.get("rule") for e in entries
        if str(e.get("provenance", "")).startswith("derived:")
    }
    assert "Bash(python3 mod.py:*)" in derived_rules
    matching = [e for e in entries if e.get("rule") == "Bash(python3 mod.py:*)"]
    assert matching and matching[0]["provenance"] == "derived:DR-O"


def test_cmd_stage_grants_reports_runtime_grant_alongside_derived_rule_idempotently(
    store, fixtures_dir,
):
    """(#4) A runtime grant recorded via `resolve-permission --scope stage` must
    show up in `cmd_stage_grants`'s flat entry list next to the stage's derived
    rule (not replacing it, not requiring a separate call), and a second read
    must return byte-identical output -- the command is a pure read, so nothing
    about calling it once should change what a second call sees."""
    sid = "stage-grants-runtime-alongside-derived"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.permission_request = cli.PermissionRequest(action="need pytest", stage_index=1, raw="need pytest")
    store.save(state)
    resolved = cli.cmd_resolve_permission(
        ns(session=sid, decision="granted", scope="stage",
           rules=["Bash(python3 -m pytest scripts/tests/test_mod.py:*)"], add_dirs=None),
        store=store,
    )
    assert resolved.ok

    first = cli.cmd_stage_grants(ns(session=sid, stage=1, json=True), store=store)
    assert first.ok
    entries = first.data["grants"]
    derived_rules = {
        e.get("rule") for e in entries if str(e.get("provenance", "")).startswith("derived:")
    }
    runtime_rules = {e.get("rule") for e in entries if e.get("provenance") == "runtime"}
    assert "Bash(python3 mod.py:*)" in derived_rules
    assert "Bash(python3 -m pytest scripts/tests/test_mod.py:*)" in runtime_rules

    second = cli.cmd_stage_grants(ns(session=sid, stage=1, json=True), store=store)
    assert second.ok
    assert second.data == first.data


def test_replan_refinement_branch_refreshes_runtime_grant_stage_title(store, fixtures_dir, monkeypatch):
    """(#8b) `cmd_replan`'s "refinement" branch retitles stage 1 in place
    (same index, new title) via `_apply_refined_stage_fields` -- a runtime
    grant already stamped with the OLD title at that index must have its
    `stage_title` refreshed to the new one in the same call, else a later
    substantive approve's re-key-by-title would find zero matches and drop
    the grant for no reason a user could see."""
    # This test is not exercising the replan-authorization gate (a bare
    # refinement replan without a prior diff presentation) -- see
    # test_replan.py's identical, module-wide `_no_replan_authorization_gate`.
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    sid = "stage-grants-refresh-title-via-replan"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.permission_request = cli.PermissionRequest(action="need pytest", stage_index=1, raw="need pytest")
    store.save(state)
    cli.cmd_resolve_permission(
        ns(session=sid, decision="granted", scope="stage",
           rules=["Bash(python3 -m pytest scripts/tests/test_mod.py:*)"], add_dirs=None),
        store=store,
    )
    state = store.load(sid)
    assert state.runtime_grants["1"][0]["stage_title"] == "Scaffold module"

    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.action == "continue"  # refinement resumes execution, no re-approval

    state = store.load(sid)
    assert state.stage(1).title == "Scaffold the module skeleton"
    assert state.runtime_grants["1"][0]["stage_title"] == "Scaffold the module skeleton"


def test_stage_grant_entries_empty_when_snapshot_bytes_dont_match_stamped_hash(store, fixtures_dir):
    # A tampered/corrupted snapshot must fail CLOSED -- no declared, no derived
    # entries -- rather than trust bytes that no longer match what was hashed
    # at approval time.
    sid = "stage-grants-tampered-snapshot"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    assert state.plan_snapshot_path and state.plan_snapshot_hash
    with open(state.plan_snapshot_path, "a", encoding="utf-8") as f:
        f.write("\n# tampered\n")

    declared, derived, dropped = cli._stage_grant_entries(state, 1)
    assert declared == []
    assert derived == []
    assert dropped == []


def test_stage_grant_entries_derived_empty_when_approved_grants_sha256_stale(store, fixtures_dir):
    # The live re-derivation must re-hash to `approved_grants_sha256` before
    # a derived entry is trusted -- a stale/mismatched digest (materialization
    # code changed underneath the approval, or state corruption) drops every
    # derived entry even though the snapshot itself is intact.
    sid = "stage-grants-stale-approved-hash"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    assert state.approved_grants_sha256
    state.approved_grants_sha256 = "0" * 64

    _declared, derived, _dropped = cli._stage_grant_entries(state, 1)
    assert derived == []


def test_cmd_resolve_permission_scope_stage_records_runtime_rule_grant(store, fixtures_dir):
    sid = "resolve-permission-stage-scope"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.permission_request = cli.PermissionRequest(action="need pytest", stage_index=1, raw="need pytest")
    store.save(state)

    directive = cli.cmd_resolve_permission(
        ns(session=sid, decision="granted", scope="stage",
           rules=["Bash(python3 -m pytest scripts/tests/test_mod.py:*)"], add_dirs=None),
        store=store,
    )
    assert directive.ok
    state = store.load(sid)
    runtime = state.runtime_grants.get("1", [])
    assert any(e.get("rule") == "Bash(python3 -m pytest scripts/tests/test_mod.py:*)" for e in runtime)


def test_cmd_resolve_permission_scope_once_records_runtime_rule_grant_with_scope_key(
    store, fixtures_dir,
):
    """Finding #9: `--scope once` must materialize `--rule`/`--add-dir` onto
    `runtime_grants` exactly like `--scope stage` does (only the lifetime
    differs), and the recorded entry must carry a `"scope"` key so
    `_consume_once_grants` can later find and expire it."""
    sid = "resolve-permission-once-scope"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.permission_request = cli.PermissionRequest(action="need pytest", stage_index=1, raw="need pytest")
    store.save(state)

    directive = cli.cmd_resolve_permission(
        ns(session=sid, decision="granted", scope="once",
           rules=["Bash(python3 -m pytest scripts/tests/test_mod.py:*)"], add_dirs=None),
        store=store,
    )
    assert directive.ok
    state = store.load(sid)
    runtime = state.runtime_grants.get("1", [])
    entry = next(e for e in runtime if e.get("rule") == "Bash(python3 -m pytest scripts/tests/test_mod.py:*)")
    assert entry["scope"] == "once"
    assert entry["consumed"] is False


def test_dispatch_consumes_once_grant_after_an_ordinary_dispatch(store, fixtures_dir):
    sid = "once-grant-consumed"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.runtime_grants["1"] = [
        {"rule": "Bash(git push origin release:*)", "provenance": "runtime",
         "consumed": False, "stage_title": state.stage(1).title, "scope": "once"},
    ]
    store.save(state)

    def runner(argv, cwd=None):
        return RunResult(0, stdout="COMPLETED: done\n")

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "COMPLETED"
    state = store.load(sid)
    assert state.runtime_grants["1"][0]["consumed"] is True


def test_dispatch_does_not_consume_once_grant_on_child_infra_failure(store, fixtures_dir):
    """Finding #9: a CHILD_INFRA_FAILURE/CHILD_EXHAUSTED launch never
    meaningfully used the once-scoped grant, so it must stay live for the
    retry the caller is expected to make."""
    sid = "once-grant-survives-infra-failure"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.runtime_grants["1"] = [
        {"rule": "Bash(git push origin release:*)", "provenance": "runtime",
         "consumed": False, "stage_title": state.stage(1).title, "scope": "once"},
    ]
    store.save(state)

    def runner(argv, cwd=None):
        return RunResult(0, stdout="CHILD_INFRA_FAILURE: transient network error\n")

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "CHILD_INFRA_FAILURE"
    state = store.load(sid)
    assert state.runtime_grants["1"][0]["consumed"] is False


def test_cmd_resolve_permission_scope_stage_refuses_invalid_rule_wholesale(store, fixtures_dir):
    sid = "resolve-permission-refused"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.permission_request = cli.PermissionRequest(action="need settings", stage_index=1, raw="need settings")
    store.save(state)

    directive = cli.cmd_resolve_permission(
        ns(session=sid, decision="granted", scope="stage",
           rules=["Bash(scripts/apply-settings.sh:*)"], add_dirs=None),
        store=store,
    )
    assert not directive.ok
    state = store.load(sid)
    assert not state.runtime_grants.get("1", [])
    # the permission_request is still pending -- nothing partially recorded
    assert state.permission_request is not None


def test_cmd_grant_stats_reports_zero_counts_on_a_clean_session(store, fixtures_dir):
    sid = "grant-stats-clean"
    _to_executing(store, sid, fixtures_dir)
    directive = cli.cmd_grant_stats(ns(session=sid, json=True), store=store)
    assert directive.ok
    assert directive.data["planning_miss_counts"] == {"asked": 0, "unasked": 0}
    assert directive.data["materialization_defects"] == []


# --- cmd_dispatch's PERMISSION-REQUEST branch: covered vs uncovered --------


def test_dispatch_permission_request_covered_by_derived_grant_routes_to_diagnosing(store, fixtures_dir):
    sid = "perm-request-covered"
    _to_executing(store, sid, fixtures_dir)

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout="PERMISSION-REQUEST: need to run the module\nRule: Bash(python3 mod.py:*)\n",
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "OVERCOME-DIFFICULTY"
    state = store.load(sid)
    assert len(state.materialization_defects) == 1
    assert state.materialization_defects[0]["evidence"] == "self-reported"
    assert state.permission_request is None


def test_dispatch_permission_request_not_covered_asks_user(store, fixtures_dir):
    sid = "perm-request-uncovered"
    _to_executing(store, sid, fixtures_dir)

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout="PERMISSION-REQUEST: need to push a branch\nRule: Bash(git push origin main:*)\n",
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "PERMISSION-REQUEST"
    state = store.load(sid)
    assert state.permission_request is not None
    assert state.permission_request.action == "need to push a branch"
    assert not state.materialization_defects
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0]["source"] == "permission-request"
    assert state.planning_misses[0]["asked_user"] is True


def test_dispatch_permission_request_redirect_segment_under_prefix_grant_asks_user(store, fixtures_dir):
    """Stage 1 already carries a derived DR-O grant `Bash(python3 mod.py:*)`
    (a wildcard "prefix" grant covering `python3 mod.py` plus any args) --
    but the child's self-reported `Rule:` line appends a bare `>` redirect
    onto that same command. `_rule_line_to_call`/`grant_covers_call` must
    still refuse it (an unresolvable segment, per
    test_bash_bare_redirect_segment_not_covered_under_a_matching_prefix_grant
    above), so this is a planning miss the user is asked about, never a
    materialization defect."""
    sid = "perm-request-redirect-uncovered"
    _to_executing(store, sid, fixtures_dir)

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout=(
                "PERMISSION-REQUEST: need to log output\n"
                "Rule: Bash(python3 mod.py > /tmp/log:*)\n"
            ),
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "PERMISSION-REQUEST"
    state = store.load(sid)
    assert not state.materialization_defects
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0]["asked_user"] is True


def test_dispatch_permission_request_command_substitution_segment_under_prefix_grant_asks_user(
    store, fixtures_dir,
):
    """Same rule, for a `$(...)` command-substitution segment appended onto
    the same derived-prefix-covered command."""
    sid = "perm-request-subshell-uncovered"
    _to_executing(store, sid, fixtures_dir)

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout=(
                "PERMISSION-REQUEST: need to log the user\n"
                "Rule: Bash(python3 mod.py $(whoami):*)\n"
            ),
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "PERMISSION-REQUEST"
    state = store.load(sid)
    assert not state.materialization_defects
    assert len(state.planning_misses) == 1
    assert state.planning_misses[0]["asked_user"] is True


def test_dispatch_permission_request_promotes_existing_transcript_miss_not_a_duplicate(
    store, fixtures_dir,
):
    """Finding #8 regression: `_classify_transcript_denials` runs BEFORE the
    PERMISSION-REQUEST branch and may already append an uncovered denial to
    `planning_misses` (source="transcript", asked_user=False). The branch
    must PROMOTE that same row (asked_user -> True) rather than appending a
    second row for the identical event."""
    sid = "perm-request-promotes-transcript-miss"
    _to_executing(store, sid, fixtures_dir)
    transcript_path = fixtures_dir / "transcript_stops" / "permission-denial.jsonl"

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout=(
                "PERMISSION-REQUEST: need to run verify-all\n"
                "Rule: Bash(python3 scripts/verify-all.py:*)\n"
            ),
            stderr=f"spawn-specialist: transcript={transcript_path}\n",
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "PERMISSION-REQUEST"
    state = store.load(sid)
    assert not state.materialization_defects
    assert len(state.planning_misses) == 1
    only = state.planning_misses[0]
    assert only["source"] == "transcript"
    assert only["asked_user"] is True


def test_dispatch_permission_request_disambiguates_between_two_transcript_misses(
    store, fixtures_dir,
):
    """Finding S2 regression: two prior transcript denials sit in
    `planning_misses` (e.g. one Bash call, one unrelated one). The
    PERMISSION-REQUEST branch's self-reported `Rule:` line must promote the
    ONE row whose raw command is a prefix match for the rule's parsed
    command, not the most-recently-appended row and not a fresh duplicate."""
    sid = "perm-request-disambiguates-two-misses"
    _to_executing(store, sid, fixtures_dir)
    transcript_path = fixtures_dir / "transcript_stops" / "two-permission-denials.jsonl"

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout=(
                "PERMISSION-REQUEST: need to run verify-all\n"
                "Rule: Bash(python3 scripts/verify-all.py:*)\n"
            ),
            stderr=f"spawn-specialist: transcript={transcript_path}\n",
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "PERMISSION-REQUEST"
    state = store.load(sid)
    assert not state.materialization_defects
    # Both transcript denials are recorded (one promoted, one untouched) --
    # never a spurious third row for the promoted one.
    assert len(state.planning_misses) == 2
    promoted = [
        row for row in state.planning_misses
        if row["command"].startswith("python3 scripts/verify-all.py")
    ]
    assert len(promoted) == 1
    assert promoted[0]["asked_user"] is True
    other = [row for row in state.planning_misses if row is not promoted[0]]
    assert len(other) == 1
    assert other[0]["asked_user"] is False


def test_dispatch_permission_request_routes_via_transcript_covered_evidence(
    store, fixtures_dir,
):
    """S3 regression: the PERMISSION-REQUEST branch's `transcript_covered` path
    (evidence="transcript") is reachable independently of `self_covered`
    (evidence="self-reported") -- the child's own marker asks about a DIFFERENT
    action with no `Rule:` line (so `self_covered` is False), but the transcript
    already recorded a denial for `python3 mod.py`, which IS covered by stage
    1's DR-O-derived `Bash(python3 mod.py:*)` rule (plan_two_stage.toml declares
    `output_artifacts = ["mod.py"]`). `_diagnose_materialization_defect` must
    fire with evidence="transcript", and no duplicate row is appended for the
    same tool_use_id."""
    sid = "perm-request-transcript-covered-evidence"
    _to_executing(store, sid, fixtures_dir)
    transcript_path = fixtures_dir / "transcript_stops" / "permission-denial-covered.jsonl"

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout="PERMISSION-REQUEST: need to do something unrelated\n",
            stderr=f"spawn-specialist: transcript={transcript_path}\n",
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "OVERCOME-DIFFICULTY"
    state = store.load(sid)
    assert len(state.materialization_defects) == 1
    assert state.materialization_defects[0]["evidence"] == "transcript"
    assert state.permission_request is None
    assert not state.planning_misses


# --- (5) B4: SETTINGS_REFERENCE_CLASSIFICATION -------------------------------

# The same shape widening_targets.enumerate_live_settings/is_live_settings
# match against a real path, applied here as a plain-text scan so this test
# needs no filesystem I/O beyond reading the repo's own source: any non-test
# .py/.sh file whose text names a `settings*.json`-shaped string is a hit.
_SETTINGS_REFERENCE_RE = re.compile(r"settings[^/\s\"'`]*\.json")

# Finding B4: every non-test file this repo's own scripts/ tree contains that
# names a `settings*.json`-shaped path, classified by what it actually does
# with that reference -- "writer" (mutates a live settings*.json document, or
# directly orchestrates a script that does), "reader" (reads/parses/greps a
# live settings*.json document without writing it), or "mention" (the string
# appears only in a docstring/comment/path-helper -- no I/O on the file's
# content). A newly added settings-writing script fails
# test_settings_reference_classification_has_no_unclassified_hit until it is
# added here too.
SETTINGS_REFERENCE_CLASSIFICATION: dict[str, str] = {
    "agentctl/cli.py": "reader",
    "agentctl/classify.py": "mention",
    "agentctl/exempt_paths.py": "mention",
    "apply-mcp-local.sh": "writer",
    "apply-settings.sh": "writer",
    "doctor.sh": "reader",
    "hook-canon-guard-wired-check.py": "reader",
    "hook-guard-canon-readonly.py": "mention",
    "hook-instructions-refresh-due.py": "reader",
    "install-reminder-hooks.sh": "writer",
    "lib/config_root.py": "mention",
    "lib/dispatch_witness_snapshot.py": "mention",
    "lib/hook_wiring.py": "writer",
    "lib/host_llm.py": "reader",
    "lib/widening_targets.py": "reader",
    "lint-settings-base.py": "mention",
    "migrate-to-isolated.sh": "mention",
    "self-diagnose.py": "reader",
    "set-context-cap.sh": "writer",
    "setup-symlinks.sh": "writer",
    "spawn-specialist.py": "writer",
    "sync-instructions-repo.sh": "writer",
    "verify-judge-isolation.py": "writer",
}


def _settings_referencing_files(scripts_root: Path) -> set[str]:
    """Every non-test `.py`/`.sh` file under `scripts_root` whose text names a
    `settings*.json`-shaped path -- the population
    SETTINGS_REFERENCE_CLASSIFICATION above must classify exactly."""
    hits: set[str] = set()
    for pattern in ("*.py", "*.sh"):
        for path in scripts_root.rglob(pattern):
            rel = path.relative_to(scripts_root)
            if rel.parts[0] == "tests":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if _SETTINGS_REFERENCE_RE.search(text):
                hits.add(rel.as_posix())
    return hits


def test_settings_reference_classification_has_no_unclassified_hit():
    scripts_root = Path(__file__).resolve().parent.parent
    hits = _settings_referencing_files(scripts_root)
    classified = set(SETTINGS_REFERENCE_CLASSIFICATION)
    unclassified = hits - classified
    assert not unclassified, (
        "settings*.json-referencing file(s) missing from "
        f"SETTINGS_REFERENCE_CLASSIFICATION: {sorted(unclassified)}"
    )
    stale = classified - hits
    assert not stale, (
        "SETTINGS_REFERENCE_CLASSIFICATION names file(s) that no longer "
        f"reference settings*.json: {sorted(stale)}"
    )


def test_settings_channel_programs_covers_every_sh_writer():
    """Every `.sh` file classified "writer" above either IS a
    SETTINGS_CHANNEL_PROGRAMS entry or is excluded from that unconditional-
    refusal set for a reason outside this table's scope (none currently are)
    -- the two lists name settings-mutating entry points from two directions
    and must not silently drift apart."""
    writer_sh_basenames = {
        Path(rel).name
        for rel, verdict in SETTINGS_REFERENCE_CLASSIFICATION.items()
        if verdict == "writer" and rel.endswith(".sh")
    }
    assert writer_sh_basenames <= widening_targets.SETTINGS_CHANNEL_PROGRAMS


# --- (6) S3: enumerate_live_settings ignores a non-four-location file ------

def test_enumerate_live_settings_ignores_non_four_location_settings_file(tmp_path, monkeypatch):
    """`enumerate_live_settings` only reports the four named locations (agent
    home, harness config root, and each of child/root cwd's `.claude` dir) --
    a settings*.json sitting in some OTHER directory (e.g. a sibling checkout
    this dispatch never touches) must never appear, even though
    `is_live_settings` (the broader refusal predicate) would still refuse a
    grant naming it directly."""
    agent_home = tmp_path / "agent_home"
    harness_home = tmp_path / "harness_home"
    child_cwd = tmp_path / "child_repo"
    root_cwd = tmp_path / "root_repo"
    other = tmp_path / "unrelated_checkout" / ".claude"
    for d in (agent_home, harness_home, child_cwd / ".claude", root_cwd / ".claude", other):
        d.mkdir(parents=True)
        (d / "settings.json").write_text("{}")

    monkeypatch.setattr(config_root, "agent_home", lambda: agent_home)
    monkeypatch.setattr(config_root, "harness_config_root", lambda: harness_home)

    found = widening_targets.enumerate_live_settings(str(child_cwd), str(root_cwd))

    other_settings = str((other / "settings.json").resolve())
    assert other_settings not in found
    assert len(found) == 4
    assert widening_targets.is_live_settings(str(other / "settings.json"))


# --- (5) cmd_resolve quality row: planning_misses_asked / planning_misses_unasked --


def test_cmd_resolve_quality_row_separates_asked_and_unasked_planning_misses(store, fixtures_dir):
    """(#12b) `state.planning_misses` mixes asked (a PERMISSION-REQUEST the
    manager surfaced to the user) and unasked (seen only via transcript
    classification) rows -- `cmd_resolve`'s quality-ledger row must count each
    kind separately, not just report a single combined total."""
    sid = "resolve-quality-planning-misses"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing(store, sid, fixtures_dir, plan_path=plan)
    state = store.load(sid)
    state.planning_misses = [
        {"stage_index": 1, "asked_user": True, "ts": "t1"},
        {"stage_index": 1, "asked_user": True, "ts": "t2"},
        {"stage_index": 2, "asked_user": False, "ts": "t3"},
    ]
    store.save(state)
    cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                             control="reviewed: ok", observation=STAGE_OBSERVATIONS[0],
                             cost_log=None), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                             control="reviewed: ok", observation=STAGE_OBSERVATIONS[1],
                             cost_log=None), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched",
                             note=""), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="skipped",
                             note="test fixture, nothing to record"), store=store)
    d = cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                           quality_note=None, cost_log=None), store=store)
    assert d.ok is True

    rows = [
        json.loads(line)
        for line in cli.TASK_QUALITY_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    row = rows[-1]
    assert row["planning_misses_asked"] == 2
    assert row["planning_misses_unasked"] == 1
