"""Oracle-first RED catalogue for the whole-PR review of PR-A (spawn-permission-
grant-model stages 1-3), recorded under the plan slug
`spawn-permission-grant-model` (verdict: REVIEW: revise).

One test (or parametrized group) per finding ID named in the review's
"## Fixes" section, covering exactly what that finding requires: B1, B2, S1,
S2, S3, S4, S10, N1. Every test that fails against the code as it stood when
this file was authored carries a strict `xfail` mark -- the ONLY
edits this file may receive after its own oracle commit are the removal of an
xfail mark once the matching fix lands (see the developer marker-protocol
brief for this stage: an oracle test believed WRONG is never silently edited,
it is a `REPLAN:`).

A second round (this file's ORACLE FIRST paragraph in stage 3's procedure
item 8) adds three more rows on top of the same catalogue, still keyed to
findings the fix-diff re-review raised: `claude` recognized only by basename
lets an install-path invocation (`.../claude/versions/<ver>`, `node .../
cli.js`, `npx @anthropic-ai/claude-code@<tag>`) through `validate_rule`;
`S10`'s repo-root-deny pairing is skipped when the child's cwd already IS the
repo root; and a handful of code-executing git subcommands are not refused,
so a wildcarded baseline rule the developer kind already carries (`Bash(git
fetch:*)`) can admit one of them.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
from argparse import Namespace

from agentctl import cli
from agentctl import grants
from agentctl.grants import (
    GrantValidationError,
    StageGrants,
    grant_covers_call,
    validate_add_dir,
    validate_rule,
)
from lib import config_root

from test_stage_grants import _to_executing, ns  # noqa: F401 -- shared test fixtures/helpers


SPAWN_SPECIALIST_PATH = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


def _load_spawn_specialist():
    spec = importlib.util.spec_from_file_location("pra_review_spawn_specialist", SPAWN_SPECIALIST_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SPAWN = _load_spawn_specialist()


# --- B1: wrapper/interpreter/git wildcard-widening mutation catalogue ------

_B1_REFUSED_RULES = [
    # wrapper-option-consumption gap: strip_wrappers only special-cases
    # env/timeout for consuming their own option+value; every other wrapper
    # token (nice/sudo/xargs/time/stdbuf/ionice/chrt/...) leaves its flag in
    # the "stripped" remainder, so the flag itself -- never a real program --
    # is what downstream checks see, and none of them refuse a bare flag.
    "Bash(nice -n:*)",
    "Bash(sudo -u root:*)",
    "Bash(xargs -0:*)",
    "Bash(time -p:*)",
    "Bash(stdbuf -o0:*)",
    "Bash(ionice -c3:*)",
    "Bash(chrt -f:*)",
    # claude-program spelling variants is_claude_program doesn't recognize.
    "Bash(npx @anthropic-ai/claude-code -p:*)",
    "Bash(claude-code:*)",
    # package-runner wrapper forms besides npx: bunx is a single-token
    # wrapper like npx; `pnpm dlx`/`pnpm exec`/`yarn dlx` are two-token
    # wrapper forms strip_wrappers does not special-case at all, so the
    # wrapper's own SECOND token (dlx/exec) surfaces as the "program name"
    # to every downstream check and none of them refuse it.
    "Bash(bunx claude:*)",
    "Bash(pnpm dlx @anthropic-ai/claude-code:*)",
    # command-name case: on a case-insensitive filesystem (macOS default) a
    # differently-cased spelling resolves to the same real binary, but every
    # classification check here compares tokens by exact string equality.
    "Bash(Claude:*)",
    "Bash(GIT -c x=y:*)",
    "Bash(Sudo -u root:*)",
    # interpreter value-taking flags that are not among the -c/-e/-p/-m
    # dangerous set validate_rule already refuses, but that still let the
    # child supply arbitrary content the rule text never pinned.
    "Bash(python3 -W ignore:*)",
    "Bash(perl -I lib:*)",
    "Bash(node --require x:*)",
    "Bash(env python3 -W x:*)",
    # git has no dedicated refusal at all: a wildcarded git root or a
    # `-c` config override admits arbitrary git-mediated code execution
    # (core.pager, alias.*, hooks via `-c core.hooksPath`, etc).
    "Bash(git:*)",
    "Bash(git config:*)",
    "Bash(git -c alias.x='!sh -c \"true\"' x:*)",
]


@pytest.mark.parametrize("rule", _B1_REFUSED_RULES)
def test_b1_wildcard_widening_mutation_refused(rule):
    with pytest.raises(GrantValidationError):
        validate_rule(rule)


_B1_POSITIVE_CONTROLS = [
    "Bash(python3 -m pytest:*)",
    "Bash(git log:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(ls:*)",
    f"Bash(python3 {SPAWN.SCRIPTS_DIR}/agentctl-cli.py plan-grants:*)",
    # round-6 regression pins: a two-token package-manager verb that is NOT
    # a launcher form (`pnpm install`/`yarn add`) must stay accepted -- only
    # `pnpm dlx`/`pnpm exec`/`yarn dlx` are wrappers.
    "Bash(pnpm install:*)",
    "Bash(yarn add:*)",
]


@pytest.mark.parametrize("rule", _B1_POSITIVE_CONTROLS)
def test_b1_positive_controls_still_accepted(rule):
    validate_rule(rule)  # must not raise


def test_b1_every_kind_baseline_rule_still_validates():
    """A B1 fix that over-refuses (e.g. blanket-refusing `git` entirely
    including `git log:*`/`git status:*`, or breaking the interpreter
    allowlist for an ordinary `-m pytest` invocation) would silently strand
    every kind's fleet-wide baseline -- this is the regression pin for that
    failure mode, not a B1-specific rule."""
    for kind, rules in SPAWN.KIND_BASELINES.items():
        for rule in rules:
            validate_rule(rule)  # must not raise, for every currently-shipped baseline rule


# --- B2: validate_add_dir must refuse relative paths in BOTH modes --------

_B2_RELATIVE_PATHS = ["../.claude-agent", "..", "rel/x"]


@pytest.mark.parametrize("path", _B2_RELATIVE_PATHS)
def test_b2_validate_add_dir_refuses_relative_path_in_write_mode_already(path):
    """Write mode already reaches the absolute-path check today -- this is
    the pre-existing-behavior control the read-mode xfail below is measured
    against, not itself part of the RED catalogue."""
    with pytest.raises(GrantValidationError):
        validate_add_dir(path, "write")


@pytest.mark.parametrize("path", _B2_RELATIVE_PATHS)
def test_b2_validate_add_dir_refuses_relative_path_in_read_mode(path):
    with pytest.raises(GrantValidationError):
        validate_add_dir(path, "read")


# --- S1: ~/.claude must be a protected root regardless of harness root ----


def test_s1_dot_claude_refused_as_write_add_dir():
    dot_claude = str(Path.home() / ".claude")
    with pytest.raises(GrantValidationError):
        validate_add_dir(dot_claude, "write")


def test_s1_edit_rule_under_dot_claude_refused_even_when_harness_root_differs():
    rule = f"Edit(//{str(Path.home() / '.claude' / 'CLAUDE.md').lstrip('/')})"
    with pytest.raises(GrantValidationError):
        validate_rule(rule)


# --- S2: case-insensitive-filesystem variants must be refused wherever the
# lowercase form is refused --------------------------------------------------

_S2_CASE_VARIANT_RULES = [
    "Edit(//home/the0/.Claude/settings.json)",
    "Edit(//home/the0/.claude/Settings.local.json)",
    "Edit(//home/user/repo/.GIT/hooks/pre-commit)",
]


@pytest.mark.parametrize("rule", _S2_CASE_VARIANT_RULES)
def test_s2_case_variant_rule_refused(rule):
    with pytest.raises(GrantValidationError):
        validate_rule(rule)


def test_s2_case_variant_add_dir_refused():
    variant = str(Path.home()) + "/.Claude"
    with pytest.raises(GrantValidationError):
        validate_add_dir(variant, "write")


# --- S3: a KIND_BASELINES-covered denial must classify as a materialization
# defect, not a planning miss ------------------------------------------------


def test_s3_kind_baseline_covered_call_is_in_effective_coverage(store, fixtures_dir):
    sid = "s3-kind-baseline-coverage"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    coverage = cli._effective_stage_grants(state, 1)
    # "Bash(git status:*)" is in every kind's _READ_ONLY_INSPECTION baseline
    # (see spawn-specialist.py KIND_BASELINES) but is never declared or
    # derivable from plan_two_stage.toml's stage 1 -- so today it is
    # NOT covered by _effective_stage_grants at all.
    assert grant_covers_call(coverage, "Bash", {"command": "git status"})


# --- S4: Edit/Read denials must be classified; a write add_dir's synthesized
# surface denies must be subtracted by grant_covers_call ---------------------


def test_s4_write_add_dir_surface_deny_not_covered_by_grant_covers_call(tmp_path):
    write_dir = tmp_path / "writedir"
    write_dir.mkdir()
    grants = StageGrants(add_dirs=[
        __import__("agentctl.grants", fromlist=["AddDirGrant"]).AddDirGrant(
            path=str(write_dir), mode="write", provenance="declared",
        )
    ])
    target = str(write_dir / "x" / "settings.json")
    assert not grant_covers_call(grants, "Edit", {"file_path": target})


# --- S10: lifted .claude/settings.local.json rules must pass through
# validate_rule; developer repo-root add-dir carries surface denies ---------


def test_s10_lifted_project_settings_rules_are_validated(tmp_path):
    project_settings = tmp_path / "settings.local.json"
    project_settings.write_text(
        '{"permissions": {"allow": ["Bash(claude:*)"], "deny": []}}',
        encoding="utf-8",
    )
    rules = SPAWN.project_settings_permission_rules(str(project_settings))
    # A lifted rule that validate_rule refuses (here: the unconditionally
    # refused bare `claude` program) must be dropped, not passed through.
    assert "Bash(claude:*)" not in rules


def test_s10_developer_repo_root_add_dir_carries_surface_denies(tmp_path):
    """REPLAN-authorized rewrite (root disposition: the oracle test was
    wrong, not the code -- the original test only probed the `--add-dir`
    argv, which never carried surface denies in the first place; the actual
    grant reaching the child is the `--settings` JSON `permissions.deny`
    array `build_child_settings` builds, paired via `repo_root_deny_rules`).
    """
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    workdir = root / "sub"
    workdir.mkdir()

    settings = SPAWN.build_child_settings("developer", workdir=str(workdir))

    deny = settings["permissions"]["deny"]
    root_real = os.path.realpath(str(root))
    base = grants.rule_file_arg(root_real)
    assert f"Edit({base}/**/.claude/**)" in deny
    assert f"Edit({base}/**/settings*.json)" in deny
    assert f"Edit({base}/**/.git/**)" in deny
    assert f"Edit({base}/**/.git)" in deny


# --- N1: PERMISSION-REQUEST coverage must be tied to its OWN requested Rule,
# never to "some" covered denial elsewhere in the same dispatch -------------


def test_n1_uncovered_permission_request_stays_an_ask_even_after_an_unrelated_covered_denial(
    store, fixtures_dir, tmp_path,
):
    """Within a SINGLE dispatch call: the transcript scan records a covered
    denial for an UNRELATED command (`python3 mod.py`, covered by stage 1's
    derived DR-O grant from its `output_artifacts = ["mod.py"]`), and the
    child's own self-reported PERMISSION-REQUEST carries a DIFFERENT,
    genuinely uncovered Rule (`git push origin main`). The request must still
    be surfaced to the user (PERMISSION-REQUEST marker), never silently
    resolved as a materialization defect just because SOME denial in this
    dispatch happened to be covered."""
    sid = "n1-unrelated-covered-denial"
    _to_executing(store, sid, fixtures_dir)

    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(
        '{"type": "assistant", "message": {"content": [{"type": "tool_use", '
        '"id": "toolu_n1_1", "name": "Bash", "input": {"command": "python3 mod.py"}}]}}\n'
        '{"type": "user", "message": {"content": [{"type": "tool_result", '
        '"tool_use_id": "toolu_n1_1", "content": "Permission to use Bash with command '
        'python3 mod.py has been denied.", "is_error": true}]}, '
        '"toolDenialKind": "permission-rule"}\n',
        encoding="utf-8",
    )

    from agentctl.dispatch import RunResult

    def runner(argv, cwd=None):
        return RunResult(
            0,
            stdout=(
                "PERMISSION-REQUEST: need to push a branch\n"
                "Rule: Bash(git push origin main:*)\n"
            ),
            stderr=f"spawn-specialist: transcript={transcript_path}\n",
        )

    directive = cli.cmd_dispatch(
        ns(session=sid, budget="medium", complexity="medium", dry_run=False),
        store=store, runner=runner,
    )
    assert directive.marker == "PERMISSION-REQUEST"
    state = store.load(sid)
    assert state.permission_request is not None
    assert state.permission_request.action == "need to push a branch"


# --- round 2: claude recognized only by basename lets an install-path
# invocation through validate_rule (rereview B1' item 1) -------------------

_INSTALL_PATH_CLAUDE_RULES = [
    "Bash(/home/u/.local/share/claude/versions/2.1.282 -p:*)",
    "Bash(/opt/x/claude/versions/9.9.9:*)",
    "Bash(node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js:*)",
    "Bash(npx @anthropic-ai/claude-code@latest:*)",
]


@pytest.mark.parametrize("rule", _INSTALL_PATH_CLAUDE_RULES)
def test_claude_recognized_by_install_path_refused(rule):
    with pytest.raises(GrantValidationError):
        validate_rule(rule)


# --- round 2: S10 deny gap when the child's cwd already IS the repo root
# (rereview should-fix "S10 deny gap") --------------------------------------


def test_s10_repo_root_workdir_itself_carries_surface_denies(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    settings = SPAWN.build_child_settings("developer", workdir=str(root))

    deny = settings.get("permissions", {}).get("deny", [])
    root_real = os.path.realpath(str(root))
    base = grants.rule_file_arg(root_real)
    assert f"Edit({base}/**/.claude/**)" in deny
    assert f"Edit({base}/**/settings*.json)" in deny
    assert f"Edit({base}/**/.git/**)" in deny
    assert f"Edit({base}/**/.git)" in deny


# --- round 2: code-executing git subcommands must not be admitted by any
# KIND_BASELINES rule (rereview B1' item 7) ---------------------------------


def _baseline_grants(kind: str) -> StageGrants:
    return StageGrants(allow=[
        __import__("agentctl.grants", fromlist=["RuleGrant"]).RuleGrant(rule=r, provenance="declared")
        for r in SPAWN.KIND_BASELINES[kind]
    ])


_CODE_EXECUTING_GIT_COMMANDS = [
    "git fetch --upload-pack=x",
    "git grep -O cat",
    "git rebase --exec x",
    "git submodule foreach x",
    # round 3 (root): narrowing to the `origin` remote does not help --
    # `Bash(git fetch origin:*)` still prefix-admits the same option.
    "git fetch origin --upload-pack=x",
]


@pytest.mark.parametrize("command", _CODE_EXECUTING_GIT_COMMANDS)
def test_code_executing_git_subcommand_not_admitted_by_any_kind_baseline(command):
    for kind in SPAWN.KIND_BASELINES:
        assert not grant_covers_call(_baseline_grants(kind), "Bash", {"command": command}), (
            f"kind {kind!r} admits {command!r}"
        )


# --- round 3 (root): the engine's coverage verdict must agree with what the
# child harness itself admits. The harness matches a Bash rule against the
# LITERAL command string (no path resolution), so an engine that treats
# `scripts/x.py` and `/abs/scripts/x.py` as equivalent while the child
# settings carry only one spelling classifies a real harness denial as a
# materialization defect. Stage 3 item (c): every baseline script rule must
# match both invocation forms -- in the materialized settings, not only in
# the engine. ----------------------------------------------------------------


def _harness_admits(allow: list[str], command: str) -> bool:
    for rule in allow:
        if not (rule.startswith("Bash(") and rule.endswith(")")):
            continue
        arg = rule[len("Bash("):-1]
        if arg.endswith(":*"):
            prefix = arg[:-2]
            if command == prefix or command.startswith(prefix + " "):
                return True
        elif command == arg:
            return True
    return False


@pytest.mark.parametrize("script", ["verify-all.py", "verify-agentctl.py", "gen_crutch_registry.py"])
@pytest.mark.parametrize("form", ["absolute", "relative"])
def test_baseline_script_rule_both_forms_materialized_and_engine_agrees(tmp_path, script, form):
    allow = SPAWN.build_child_settings("developer", workdir=str(tmp_path))["permissions"]["allow"]
    path = f"{SPAWN.SCRIPTS_DIR}/{script}" if form == "absolute" else f"scripts/{script}"
    command = f"python3 {path} --flag"
    harness = _harness_admits(allow, command)
    engine = grant_covers_call(_baseline_grants("developer"), "Bash", {"command": command})
    assert harness, f"child settings do not admit {command!r}"
    assert engine == harness
