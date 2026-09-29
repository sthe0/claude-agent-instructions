"""Guards the cd-free `agentctl-cli.py` invocation prescribed by CLAUDE.md, the
cursor mirror and scripts/agentctl/README.md (a shell `cd` persists across a
spawned child's later Bash calls), the "## The engine belongs to the parent"
brief section every spawned kind receives, and the read-only KIND_BASELINES
grants for the `agentctl-cli.py classify`/`status` spelling."""
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


def test_read_only_kind_baseline_covers_agentctl_cli_status_and_classify():
    from lib import kind_baselines

    abs_scripts = kind_baselines.REPO_ROOT / "scripts"
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


def _engine_section(prompt: str) -> str:
    head = "## The engine belongs to the parent"
    assert head in prompt
    rest = prompt.split(head, 1)[1]
    return rest.split("\n## ", 1)[0]


def test_engine_belongs_to_parent_section_present_for_every_kind(tmp_path):
    from lib import kind_baselines, widening_targets

    for kind in kind_baselines.KIND_BASELINES:
        args = _args(tmp_path, kind=kind)
        section = _engine_section(MOD.assemble_prompt(args, depth=1, permissions=""))
        assert "AGENTCTL_USER_AUTHORITY_VERBS" in section, kind
        assert "return a marker" in section, kind
        named = section.split("scripts/lib/widening_targets.py:", 1)[1]
        named = named.split("and the rest of that set", 1)[0]
        verbs = [v.strip() for v in named.split(",") if v.strip()]
        assert verbs, kind
        for verb in verbs:
            assert verb in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS, (kind, verb)


def test_planner_brief_keeps_plan_grants_and_names_user_authority_verbs(tmp_path):
    args = _args(tmp_path, kind="planner")
    prompt = MOD.assemble_prompt(args, depth=1, permissions="")
    assert "agentctl-cli.py plan-grants" in prompt
    assert "AGENTCTL_USER_AUTHORITY_VERBS" in prompt


def test_no_kind_baseline_agentctl_rule_is_verbless_or_user_authority():
    import shlex

    from lib import kind_baselines, widening_targets

    for kind, rules in kind_baselines.KIND_BASELINES.items():
        for rule in rules:
            if not rule.startswith("Bash(") or "agentctl" not in rule:
                continue
            body = rule[len("Bash("):-1]
            if body.endswith(":*"):
                body = body[:-2]
            invokes, verb = widening_targets.agentctl_invocation_verb(shlex.split(body))
            if not invokes:
                continue
            assert verb is not None, (kind, rule)
            assert verb not in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS, (kind, rule)
