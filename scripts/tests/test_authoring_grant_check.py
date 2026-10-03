"""The authoring-time grant-collision check (`agentctl/grant_shadow.py`).

`ag*` cases pin `plan_grant_shadow_problems` and its two seams (the submission
refusal and `plan-grants`): a spawn stage whose declared + derived grants make
`build_child_settings` refuse its child settings is refused when the plan is
written, with the spawn's own message. `mx*` cases pin the advisory companion
`mixed_spelling_advisories`: one directory spelled both absolute and
venue-relative inside one stage is reported, never refused.

`grant_shadow` is imported inside each case, not at module top, so a tree that
lacks the module fails every case individually instead of failing collection —
`ag_mutation_control.py`'s S0 subject relies on that.
"""
import importlib.util
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, grants, render
from agentctl.plan import _venue_for, load_plan
from agentctl.submission import submission_violations
from conftest import SUBSTANTIVE_FINAL_CHECK, SUBSTANTIVE_ORDER
from test_replan import _to_executing_stage1

SCRIPTS_DIR = Path(__file__).resolve().parent.parent

BASELINE = ".ccgram/relay-health-baseline.json"


def _gs():
    from agentctl import grant_shadow

    return grant_shadow


def _spawn_specialist():
    spec = importlib.util.spec_from_file_location(
        "spawn_specialist_authoring_test", SCRIPTS_DIR / "spawn-specialist.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _toml_list(items):
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


def _stage(index, title, *, executor="spawn:developer", material_refs=(), knowledge_refs=(),
           output_artifacts=(), extra=""):
    return f"""
[[stage]]
index = {index}
title = "{title}"
executor = "{executor}"
expected_result_image = "i"
criterion_type = "measurable"
done_criterion = "d"
depends_on = []
material = "m (traces to R1)"
material_refs = {_toml_list(material_refs)}
knowledge_refs = {_toml_list(knowledge_refs)}
output_artifacts = {_toml_list(output_artifacts)}
knowledge = "kn"
means = "me"
method = "mt"
procedure = "1. read the fixture. 2. apply the edit. 3. re-check the seam"
invariants = "inv"
capability_required = "c"
conditions = "co"
preconditions = "p"
verify_command = "pytest -q"
negative_control = "false"

[stage.principle]
statement = "s"
source = "src"
derivation = "a distinct derivation clause"
confidence = "high"
refutation = "r"
{extra}"""


def _plan(path, stages, *, repo_root, delivery_worktree=None, weight_class="substantive"):
    meta = [
        "[meta]",
        'task_id = "demo"',
        'goal = "g"',
        'done_criterion = "dc"',
        'criterion_type = "measurable"',
        f'weight_class = "{weight_class}"',
        'external_research = "n/a"',
        f'repo_root = "{repo_root}"',
    ]
    if delivery_worktree is not None:
        meta.append(f'delivery_worktree = "{delivery_worktree}"')
    order = SUBSTANTIVE_ORDER if weight_class == "substantive" else ""
    final = SUBSTANTIVE_FINAL_CHECK if weight_class == "substantive" else ""
    path.write_text("\n".join(meta) + "\n" + order + "".join(stages) + final, encoding="utf-8")
    return path


@pytest.fixture
def venue(tmp_path):
    v = Path(tmp_path).resolve() / "venue"
    v.mkdir()
    return v


def _collision_stage(venue, index=1, title="Write the baseline", **kw):
    return _stage(index, title, material_refs=[BASELINE],
                  knowledge_refs=[f"{venue}/.ccgram/state.json"], **kw)


def _clean_stage(venue, index=1, title="Write elsewhere"):
    return _stage(index, title, material_refs=["src/a.py"],
                  output_artifacts=[f"{venue.parent}/outside/report.json"])


def _git(*args):
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def _git_repo(path):
    path.mkdir(parents=True)
    _git("-C", str(path), "init", "-q")
    return path


def test_ag1_collision_reported_with_both_derivations(venue, tmp_path):
    doc = load_plan(_plan(tmp_path / "p.toml", [_collision_stage(venue)], repo_root=venue))
    problems = _gs().plan_grant_shadow_problems(doc)
    assert len(problems) == 1
    assert problems[0].startswith("stage 1 (Write the baseline):")
    assert "derived:DR-E" in problems[0]
    assert "derived:DR-R" in problems[0]


def test_ag2_problem_text_is_build_child_settings_refusal(venue, tmp_path):
    doc = load_plan(_plan(tmp_path / "p.toml", [_collision_stage(venue)], repo_root=venue))
    stage = doc.stages[0]
    derived, _dropped = grants.derive_stage_grants(stage, venue=str(venue))
    entries = [r.to_dict() for r in derived.allow] + [a.to_dict() for a in derived.add_dirs]
    spawn = _spawn_specialist()
    with pytest.raises(spawn.GrantShadowError) as refused:
        spawn.build_child_settings("developer", engine_grants=entries, workdir=str(venue))
    prefix = "stage 1 (Write the baseline): "
    problems = _gs().plan_grant_shadow_problems(doc)
    assert len(problems) == 1 and problems[0].startswith(prefix)
    assert problems[0][len(prefix):] == str(refused.value)


def test_ag3_clean_stage_has_no_problem(venue, tmp_path):
    doc = load_plan(_plan(tmp_path / "p.toml", [_clean_stage(venue)], repo_root=venue))
    assert _gs().plan_grant_shadow_problems(doc) == []


def test_ag4_declared_write_add_dir_resolves_collision(venue, tmp_path):
    extra = f"""
[stage.grants]
add_dirs = [{{ path = "{venue}/.ccgram", mode = "write" }}]
"""
    doc = load_plan(_plan(tmp_path / "p.toml", [_collision_stage(venue, extra=extra)], repo_root=venue))
    assert _gs().plan_grant_shadow_problems(doc) == []


def test_ag5_collision_refused_whatever_the_weight_class(venue, tmp_path):
    doc = load_plan(_plan(tmp_path / "p.toml", [_collision_stage(venue)], repo_root=venue,
                          weight_class="small_change"))
    out = submission_violations(doc)
    assert any(p.startswith("stage 1 (Write the baseline):") and "derived:DR-E" in p for p in out)


def test_ag6_plan_grants_exits_one_on_collision(venue, tmp_path, capsys):
    bad = _plan(tmp_path / "bad.toml", [_collision_stage(venue)], repo_root=venue)
    rc = cli.main(["--state-root", str(tmp_path / "state"), "plan-grants", "--plan", str(bad),
                   "--format", "compact"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "DR-E" in out and "DR-R" in out
    good = _plan(tmp_path / "good.toml", [_clean_stage(venue)], repo_root=venue)
    assert cli.main(["--state-root", str(tmp_path / "state"), "plan-grants", "--plan", str(good),
                     "--format", "compact"]) == 0


def test_ag7_plan_grants_json_carries_shadow_problems(venue, tmp_path):
    bad = _plan(tmp_path / "bad.toml", [_collision_stage(venue)], repo_root=venue)
    d = render.cmd_plan_grants(Namespace(plan=str(bad), format="json"))
    expected = _gs().plan_grant_shadow_problems(load_plan(bad))
    assert len(expected) == 1
    assert d.data["shadow_problems"] == expected
    good = _plan(tmp_path / "good.toml", [_clean_stage(venue)], repo_root=venue)
    assert render.cmd_plan_grants(Namespace(plan=str(good), format="json")).data["shadow_problems"] == []


def test_ag8_in_thread_stage_is_not_checked(venue, tmp_path):
    allow = f"Edit({grants.rule_file_arg(f'{venue}/{BASELINE}')})"
    extra = f"""
[stage.grants]
allow = ["{allow}"]
"""
    doc = load_plan(_plan(tmp_path / "p.toml",
                          [_collision_stage(venue, executor="in_thread", extra=extra)], repo_root=venue))
    assert _gs().plan_grant_shadow_problems(doc) == []


def test_ag9_every_spawn_stage_is_checked(venue, tmp_path):
    stages = [_clean_stage(venue, 1, "Clean first"), _collision_stage(venue, 2, "Collide second")]
    doc = load_plan(_plan(tmp_path / "p.toml", stages, repo_root=venue))
    problems = _gs().plan_grant_shadow_problems(doc)
    assert len(problems) == 1
    assert problems[0].startswith("stage 2 (Collide second):")


def test_ag10_workdir_is_the_plan_venue_not_the_authors_cwd(tmp_path, monkeypatch):
    outer = Path(tmp_path).resolve() / "outer"
    repo = _git_repo(outer / "repo")
    (repo / ".claude" / "worktrees" / "w").mkdir(parents=True)
    stage = _stage(1, "Write in nested worktree", material_refs=["repo/.claude/worktrees/w/f.py"])
    doc = load_plan(_plan(tmp_path / "p.toml", [stage], repo_root=outer))
    monkeypatch.chdir(repo)
    assert _gs().plan_grant_shadow_problems(doc) == []
    monkeypatch.chdir(tmp_path)
    assert _gs().plan_grant_shadow_problems(doc) == []


def test_ag11_cli_submission_seam_returns_the_problem(venue, tmp_path):
    doc = load_plan(_plan(tmp_path / "p.toml", [_collision_stage(venue)], repo_root=venue))
    problems = cli._submission_problems(doc, None, "substantive")
    expected = _gs().plan_grant_shadow_problems(doc)
    assert len(expected) == 1
    assert expected[0] in problems


def test_ag12_invalid_add_dir_is_a_problem_not_a_raise(venue, tmp_path):
    extra = f"""
[stage.grants]
add_dirs = [{{ path = "{venue}/a/../b", mode = "read" }}]
"""
    doc = load_plan(_plan(tmp_path / "p.toml", [_clean_stage(venue) + extra],
                          repo_root=venue))
    stage = doc.stages[0]
    declared, derived, _dropped = render._stage_declared_and_derived_grants(stage, str(venue))
    entries = [r.to_dict() for r in declared.allow] + [a.to_dict() for a in declared.add_dirs]
    entries += [r.to_dict() for r in derived.allow] + [a.to_dict() for a in derived.add_dirs]
    spawn = _spawn_specialist()
    with pytest.raises(grants.GrantValidationError) as refused:
        spawn.build_child_settings("developer", engine_grants=entries, workdir=str(venue))
    problems = _gs().plan_grant_shadow_problems(doc)
    assert problems == [f"stage 1 (Write elsewhere): {refused.value}"]


def test_ag13_uncreated_delivery_worktree_is_not_replaced_by_repo_root(tmp_path):
    repo = _git_repo(Path(tmp_path).resolve() / "repo")
    worktree = repo / ".claude" / "worktrees" / "w"
    stage = _stage(1, "Write in the worktree", material_refs=["src/f.py"])
    doc = load_plan(_plan(tmp_path / "p.toml", [stage], repo_root=repo, delivery_worktree=worktree))

    assert not worktree.exists()
    assert _gs().plan_grant_shadow_problems(doc) == []
    _declared, derived, _dropped = render._stage_declared_and_derived_grants(doc.stages[0], _venue_for(doc))
    assert f"Edit({grants.rule_file_arg(f'{worktree}/src/f.py')})" in [r.rule for r in derived.allow]

    # A real linked worktree, not a mkdir: `git rev-parse --show-toplevel` inside a
    # plain mkdir'd directory resolves to the enclosing repo, whose `.claude` guard
    # would then deny the write — so a correct implementation would redden here.
    (repo / "README").write_text("r", encoding="utf-8")
    _git("-C", str(repo), "add", "README")
    _git("-C", str(repo), "commit", "-q", "-m", "init")
    _git("-C", str(repo), "worktree", "add", "-q", str(worktree))
    assert _gs().plan_grant_shadow_problems(doc) == []


def test_ag14_repo_root_guard_deny_collision_names_its_source(tmp_path):
    repo = _git_repo(Path(tmp_path).resolve() / "repo")
    stage = _stage(1, "Write into the guard", material_refs=[".claude/scratch/a.json"])
    doc = load_plan(_plan(tmp_path / "p.toml", [stage], repo_root=repo))
    problems = _gs().plan_grant_shadow_problems(doc)
    assert len(problems) == 1
    assert "repo_root_deny_rules" in problems[0]


def test_known_bad_fixture_is_refused_by_plan_grants(tmp_path, capsys):
    fixture = Path(__file__).resolve().parent / "known_bad_grant_collision.toml"
    rc = cli.main(["--state-root", str(tmp_path / "state"), "plan-grants", "--plan", str(fixture),
                   "--format", "compact"])
    out = capsys.readouterr().out
    assert rc != 0
    assert "derived:DR-E" in out and "derived:DR-R" in out


def _advisories(path):
    return _gs().mixed_spelling_advisories(load_plan(path, strict=False))


def test_mx1_same_file_both_spellings(venue, tmp_path):
    stage = _stage(1, "Mixed", executor="spawn:thinker", material_refs=[".ccgram/a.json"],
                   output_artifacts=[f"{venue}/.ccgram/a.json"])
    adv = _advisories(_plan(tmp_path / "p.toml", [stage], repo_root=venue))
    assert len(adv) == 1
    assert adv[0].startswith("stage 1 (Mixed):")
    assert "'.ccgram/a.json'" in adv[0]
    assert f"'{venue}/.ccgram/a.json'" in adv[0]
    assert "spell both relative" in adv[0]


def test_mx2_two_files_same_directory(venue, tmp_path):
    stage = _stage(1, "Mixed", executor="spawn:thinker", material_refs=[".ccgram/a.json"],
                   output_artifacts=[f"{venue}/.ccgram/b.json"])
    assert len(_advisories(_plan(tmp_path / "p.toml", [stage], repo_root=venue))) == 1


def test_mx3_knowledge_refs_count(venue, tmp_path):
    stage = _stage(1, "Mixed", executor="spawn:thinker", material_refs=[".ccgram/a.json"],
                   knowledge_refs=[f"{venue}/.ccgram/b.json"])
    assert len(_advisories(_plan(tmp_path / "p.toml", [stage], repo_root=venue))) == 1


def test_mx4_uniform_spelling_is_silent(venue, tmp_path):
    stages = [
        _stage(1, "All relative", executor="spawn:thinker",
               material_refs=[".ccgram/a.json", ".ccgram/b.json"], output_artifacts=["src/c.py"]),
        _stage(2, "All absolute", executor="spawn:thinker",
               knowledge_refs=[f"{venue}/.ccgram/a.json", f"{venue}/.ccgram/b.json"]),
    ]
    assert _advisories(_plan(tmp_path / "p.toml", stages, repo_root=venue)) == []


def test_mx5_pairs_are_per_stage(venue, tmp_path):
    stages = [
        _stage(1, "Relative", executor="spawn:thinker", material_refs=[".ccgram/a.json"]),
        _stage(2, "Absolute", executor="spawn:thinker", knowledge_refs=[f"{venue}/.ccgram/b.json"]),
    ]
    assert _advisories(_plan(tmp_path / "p.toml", stages, repo_root=venue)) == []


def test_mx6_directory_is_compared_by_segment(venue, tmp_path):
    stage = _stage(1, "Siblings", executor="spawn:thinker", material_refs=["binx/b.sh"],
                   knowledge_refs=[f"{venue}/bin/a.sh"])
    assert _advisories(_plan(tmp_path / "p.toml", [stage], repo_root=venue)) == []


def _mx7_plan(path, venue):
    stage = f"""
[[stage]]
index = 1
title = "Review the relay"
executor = "spawn:thinker"
expected_result_image = "review written"
criterion_type = "measurable"
done_criterion = "review present"
depends_on = []
material_refs = [".ccgram/a.json"]
knowledge_refs = ["{venue}/.ccgram/b.json"]
"""
    path.write_text(f"""[meta]
weight_class = "small_change"
task_id = "demo-two-stage"
goal = "Review the relay state"
done_criterion = "review present"
criterion_type = "measurable"
repo_root = "{venue}"
{stage}""", encoding="utf-8")
    return path


def test_mx7_advisory_is_not_a_submission_problem(venue, tmp_path):
    doc = load_plan(_mx7_plan(tmp_path / "p.toml", venue))
    assert _gs().mixed_spelling_advisories(doc)
    assert submission_violations(doc) == []


def test_mx8_plan_grants_prints_advisory_and_exits_zero(venue, tmp_path, capsys):
    plan = _mx7_plan(tmp_path / "p.toml", venue)
    rc = cli.main(["--state-root", str(tmp_path / "state"), "plan-grants", "--plan", str(plan),
                   "--format", "compact"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "advisory: stage 1 (Review the relay):" in out


def test_mx9_submit_plan_carries_advisory(venue, tmp_path, store):
    plan = str(_mx7_plan(tmp_path / "p.toml", venue))
    sid = "mx9"
    cli.cmd_start(Namespace(session=sid, task="demo-two-stage", goal="", done_criterion="",
                            criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(Namespace(session=sid, chat=False, changed_lines=200, files=5,
                               wall_clock_min=60, tracker_key=None, architectural=True,
                               external_effect=False, new_dependency=False,
                               public_api_change=False), store=store)
    cli.cmd_plan(Namespace(session=sid), store=store)
    d = cli.cmd_submit_plan(Namespace(session=sid, plan=plan), store=store)
    assert d.ok
    expected = _gs().mixed_spelling_advisories(load_plan(plan))
    assert expected and all(a in d.data["advisories"] for a in expected)


def test_mx10_replan_carries_advisory(venue, tmp_path, store, fixtures_dir, monkeypatch):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    sid = "mx10"
    _to_executing_stage1(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    text = (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    text = text.replace('criterion_type = "measurable"\n\n[[stage]]',
                        f'criterion_type = "measurable"\nrepo_root = "{venue}"\n\n[[stage]]', 1)
    text = text.replace('output_artifacts = ["mod.py"]',
                        f'output_artifacts = ["mod.py"]\nmaterial_refs = [".ccgram/a.json"]\n'
                        f'knowledge_refs = ["{venue}/.ccgram/b.json"]', 1)
    new = tmp_path / "replanned.toml"
    new.write_text(text, encoding="utf-8")
    expected = _gs().mixed_spelling_advisories(load_plan(new))
    assert len(expected) == 1
    d = cli.cmd_replan(Namespace(session=sid, plan=str(new)), store=store)
    assert expected[0] in d.data["advisories"]


def test_mx11_nested_directory_counts_and_collides(venue, tmp_path):
    stage = _stage(1, "Nested", material_refs=[".ccgram/sub/b.json"],
                   knowledge_refs=[f"{venue}/.ccgram/a.json"])
    path = _plan(tmp_path / "p.toml", [stage], repo_root=venue)
    assert len(_advisories(path)) == 1
    problems = _gs().plan_grant_shadow_problems(load_plan(path))
    assert len(problems) == 1 and problems[0].startswith("stage 1 (Nested):")
