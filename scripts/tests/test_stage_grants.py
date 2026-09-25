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

import pytest
from argparse import Namespace

from agentctl import cli
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
from lib import config_root


def ns(**kw):
    return Namespace(**kw)


# --- (1) validate_rule accept/refuse table ---------------------------------

_ACCEPTED_RULES = [
    "Bash(git status:*)",
    "Bash(git status)",
    "Bash(python3 scripts/tests/test_foo.py:*)",
    "Bash(python3 -m pytest scripts/tests -q:*)",
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


def _to_executing(store, sid, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
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


def test_cmd_stage_grants_reports_derived_dr_o_for_stage_one(store, fixtures_dir):
    sid = "stage-grants-report"
    _to_executing(store, sid, fixtures_dir)
    directive = cli.cmd_stage_grants(ns(session=sid, stage=1, json=True), store=store)
    assert directive.ok
    derived_rules = {e.get("rule") for e in directive.data["grants"]["derived"]}
    assert "Bash(python3 mod.py:*)" in derived_rules


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
