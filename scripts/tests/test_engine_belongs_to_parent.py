"""Guards Issue #267's fix: the cd-free `agentctl-cli.py` invocation form now
prescribed by CLAUDE.md, the cursor mirror and scripts/agentctl/README.md,
replacing `cd ~/claude-agent-instructions/scripts && python3 -m agentctl
<cmd>` — a shell `cd` persists across a spawned child's later Bash calls, so
the old form silently changed the base every later repo-relative command and
Bash-rule grant match was resolved against. Also guards the companion
"## The engine belongs to the parent" brief section (every spawned kind, not
just developer/planner) and the read-only KIND_BASELINES grant for the new
`agentctl-cli.py classify`/`status` spelling.

test_prose_uses_cd_free_form and
test_planner_brief_keeps_plan_grants_and_names_user_authority_verbs are the
negative control's anchor: dropped onto commit 9c9ba00 (the base predating
this fix), both must fail by assertion, not import error — every symbol this
file imports already exists on that base."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SPAWN_SCRIPT = REPO_ROOT / "scripts" / "spawn-specialist.py"

_PROSE_FILES = (
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "cursor" / "rules" / "claude-code-sync.mdc",
    REPO_ROOT / "scripts" / "agentctl" / "README.md",
)

_OLD_CD_FORM = "scripts && python3 -m agentctl"

_CLAUDE_MD_MAX_CHARS = 37965


def _load_spawn_specialist():
    spec = importlib.util.spec_from_file_location(
        "spawn_specialist_engine_belongs_to_parent", SPAWN_SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_spawn_specialist()


def _args(tmp_path, **overrides):
    plan = tmp_path / "plan.toml"
    plan.write_text('[meta]\ntask_id = "t"\n', encoding="utf-8")
    base = dict(
        plan=plan,
        constraints="",
        context_dossier=None,
        done_criterion="do the thing",
        criterion_type="measurable",
        continue_worktree=None,
        kind="developer",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_prose_uses_cd_free_form():
    for path in _PROSE_FILES:
        text = path.read_text(encoding="utf-8")
        assert _OLD_CD_FORM not in text, f"{path}: still prescribes the cd-based form"
        assert "agentctl-cli.py" in text, f"{path}: missing the cd-free agentctl-cli.py form"


def test_claude_md_within_char_ceiling():
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert len(text) <= _CLAUDE_MD_MAX_CHARS


def test_read_only_kind_baseline_covers_agentctl_cli_status_and_classify():
    from lib import kind_baselines

    abs_scripts = kind_baselines.REPO_ROOT / "scripts"
    # thinker: no dedicated additions beyond the shared read-only bucket, so its
    # row is the cleanest place to check the new grant landed at all.
    thinker = kind_baselines.KIND_BASELINES["thinker"]
    assert f"Bash(python3 {abs_scripts}/agentctl-cli.py classify:*)" in thinker
    assert f"Bash(python3 {abs_scripts}/agentctl-cli.py status:*)" in thinker
    assert "Bash(python3 scripts/agentctl-cli.py classify:*)" in thinker
    assert "Bash(python3 scripts/agentctl-cli.py status:*)" in thinker
    # the old `-m agentctl` forms stay -- this is an added grant, not a replacement.
    assert "Bash(python3 -m agentctl classify:*)" in thinker
    assert "Bash(python3 -m agentctl status:*)" in thinker
    # planner's cwd is normally outside this repo (see kind_baselines.py's own
    # SCRIPTS_DIR rationale), so it gets the absolute form only -- never the
    # repo-relative one, matching every other planner rule in its row.
    planner = kind_baselines.KIND_BASELINES["planner"]
    assert f"Bash(python3 {abs_scripts}/agentctl-cli.py classify:*)" in planner
    assert f"Bash(python3 {abs_scripts}/agentctl-cli.py status:*)" in planner
    assert "Bash(python3 scripts/agentctl-cli.py classify:*)" not in planner
    assert "Bash(python3 scripts/agentctl-cli.py status:*)" not in planner


def test_engine_belongs_to_parent_section_present_for_every_kind(tmp_path):
    for kind in ("developer", "thinker", "planner", "code-reviewer", "tech-writer"):
        args = _args(tmp_path, kind=kind)
        prompt = MOD.assemble_prompt(args, depth=1, permissions="")
        assert "## The engine belongs to the parent" in prompt, kind
        assert "AGENTCTL_USER_AUTHORITY_VERBS" in prompt, kind


def test_planner_brief_keeps_plan_grants_and_names_user_authority_verbs(tmp_path):
    args = _args(tmp_path, kind="planner")
    prompt = MOD.assemble_prompt(args, depth=1, permissions="")
    assert "agentctl-cli.py plan-grants" in prompt
    assert "AGENTCTL_USER_AUTHORITY_VERBS" in prompt


def test_no_kind_baseline_rule_admits_a_user_authority_verb():
    from lib import kind_baselines, widening_targets

    for kind, rules in kind_baselines.KIND_BASELINES.items():
        for rule in rules:
            if "agentctl" not in rule:
                continue
            for verb in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS:
                assert f"agentctl {verb}" not in rule, (kind, rule, verb)
                assert f"agentctl-cli.py {verb}" not in rule, (kind, rule, verb)
