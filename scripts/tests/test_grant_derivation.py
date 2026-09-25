"""Unit coverage for `grants.derive_stage_grants` (DR-V/DR-O/DR-E/DR-R) — the
pure function from a stage's declared elements to the grant set a spawned
child receives, per stage 1's plan procedure step 6.

Every proposed entry passes through the SAME validator entries pass through
when declared (grants.validate_rule/validate_add_dir); a refused entry lands
in `dropped`, never in `allow`/`add_dirs`, and derivation never raises.
"""
from __future__ import annotations

from pathlib import Path

from agentctl import plan as plan_mod
from agentctl.grants import derive_stage_grants
from agentctl.state import Actor, Criterion, Means, Stage, Subject

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "grant_plans"


def _stage(
    *,
    executor: str = "spawn:developer",
    verify_command: str | None = None,
    output_artifacts: list[str] | None = None,
    material_refs: list[str] | None = None,
    knowledge_refs: list[str] | None = None,
) -> Stage:
    return Stage(
        index=1,
        title="probe",
        subject=Subject(
            material="m",
            result="r",
            material_refs=material_refs or [],
            knowledge_refs=knowledge_refs or [],
        ),
        means=Means(means="Edit", method="apply"),
        actor=Actor(executor=executor),
        criterion=Criterion(
            criterion_type="measurable",
            done_criterion="d",
            verify_command=verify_command,
        ),
        output_artifacts=output_artifacts or [],
    )


def _rules(grants) -> set[str]:
    return {g.rule for g in grants.allow}


def _dirs(grants) -> set[tuple[str, str]]:
    return {(g.path, g.mode) for g in grants.add_dirs}


# --- DR-V: one Bash(<segment>:*) per top-level verify_command segment -----


def test_dr_v_single_segment():
    stage = _stage(verify_command="python3 -m pytest scripts/tests/test_foo.py -q")
    grants, dropped = derive_stage_grants(stage, venue="/repo")
    assert "Bash(python3 -m pytest scripts/tests/test_foo.py -q:*)" in _rules(grants)
    assert all(d["entry"] != "python3 -m pytest scripts/tests/test_foo.py -q" for d in dropped)


def test_dr_v_multiple_top_level_segments():
    stage = _stage(verify_command="black --check scripts/\nruff check scripts/")
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    rules = _rules(grants)
    assert "Bash(black --check scripts/:*)" in rules
    assert "Bash(ruff check scripts/:*)" in rules


def test_dr_v_strips_leading_bang():
    stage = _stage(verify_command="!git diff --stat")
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert "Bash(git diff --stat:*)" in _rules(grants)


def test_dr_v_drops_unresolvable_segment():
    stage = _stage(verify_command="echo $FOO")
    grants, dropped = derive_stage_grants(stage, venue="/repo")
    assert not _rules(grants)
    assert not dropped  # never proposed, so never appears as a validator refusal either


def test_dr_v_absent_when_no_verify_command():
    stage = _stage(verify_command=None)
    grants, dropped = derive_stage_grants(stage, venue="/repo")
    assert not _rules(grants)
    assert not dropped


# --- DR-O: in-venue .py/.sh output_artifacts, tests/ excluded --------------


def test_dr_o_py_output_artifact():
    stage = _stage(output_artifacts=["scripts/foo.py"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert "Bash(python3 scripts/foo.py:*)" in _rules(grants)


def test_dr_o_sh_output_artifact():
    stage = _stage(output_artifacts=["scripts/foo.sh"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert "Bash(scripts/foo.sh:*)" in _rules(grants)


def test_dr_o_excludes_tests_prefixed_artifact():
    stage = _stage(output_artifacts=["tests/test_foo.py"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert not any(r.startswith("Bash(") for r in _rules(grants))


def test_dr_o_excludes_artifact_with_tests_as_any_path_component():
    # "tests" appears as a middle component, not just a leading prefix.
    stage = _stage(output_artifacts=["scripts/tests/test_foo.py"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert not any(r.startswith("Bash(") for r in _rules(grants))


def test_dr_o_skips_outside_venue_artifact():
    stage = _stage(output_artifacts=["/etc/foo.py"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert "Bash(python3 /etc/foo.py:*)" not in _rules(grants)


def test_dr_o_ignores_non_py_sh_extension():
    stage = _stage(output_artifacts=["docs/foo.md"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert not any(r.startswith("Bash(") for r in _rules(grants))


# --- DR-E: Edit(//<abs>) only for spawn:developer/spawn:tech-writer -------


def test_dr_e_developer_gets_edit_grant_for_output_artifact():
    stage = _stage(executor="spawn:developer", output_artifacts=["scripts/foo.py"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    # Double leading slash, not triple: the absolute-path Edit rule form is
    # literally "//" + path.lstrip("/") (spawn-specialist.py's own parsing).
    assert "Edit(//repo/scripts/foo.py)" in _rules(grants)
    assert "Edit(///repo/scripts/foo.py)" not in _rules(grants)


def test_dr_e_tech_writer_gets_edit_grant_for_material_ref():
    stage = _stage(executor="spawn:tech-writer", material_refs=["docs/README.md"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert "Edit(//repo/docs/README.md)" in _rules(grants)
    assert "Edit(///repo/docs/README.md)" not in _rules(grants)


def test_dr_e_absent_for_non_developer_non_writer_executor():
    stage = _stage(executor="in_thread", output_artifacts=["scripts/foo.py"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert not any(r.startswith("Edit(") for r in _rules(grants))


def test_dr_e_skips_outside_venue_output_artifact():
    stage = _stage(executor="spawn:developer", output_artifacts=["/etc/foo.py"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert not any(r.startswith("Edit(") for r in _rules(grants))
    # and no DR-O write grant is derived for an outside-venue artifact either
    assert "Bash(python3 /etc/foo.py:*)" not in _rules(grants)


# --- DR-R: outside-venue absolute refs -> READ add_dir on their directory --


def test_dr_r_read_add_dir_for_outside_material_ref():
    stage = _stage(material_refs=["/home/user/notes/plan.md"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert ("/home/user/notes", "read") in _dirs(grants)


def test_dr_r_read_add_dir_for_outside_knowledge_ref():
    stage = _stage(knowledge_refs=["/opt/data/schema.json"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert ("/opt/data", "read") in _dirs(grants)


def test_dr_r_dedups_same_directory():
    stage = _stage(
        material_refs=["/opt/data/a.json"],
        knowledge_refs=["/opt/data/b.json"],
    )
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    dirs = [g for g in grants.add_dirs if g.path == "/opt/data"]
    assert len(dirs) == 1


def test_dr_r_skips_in_venue_ref():
    stage = _stage(material_refs=["scripts/inside.py"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert not grants.add_dirs


# --- cross-cutting: no derived permission_mode, dropped routing -----------


def test_derivation_never_sets_permission_mode():
    stage = _stage(verify_command="pytest", output_artifacts=["scripts/foo.py"])
    grants, _dropped = derive_stage_grants(stage, venue="/repo")
    assert grants.permission_mode is None


def test_validator_refused_derived_entry_lands_in_dropped_not_allow():
    # A settings-channel-program output_artifact: DR-O proposes it like any
    # other in-venue .sh artifact, but validate_rule refuses it.
    stage = _stage(output_artifacts=["scripts/install-reminder-hooks.sh"])
    grants, dropped = derive_stage_grants(stage, venue="/repo")
    assert "Bash(scripts/install-reminder-hooks.sh:*)" not in _rules(grants)
    assert any(
        d["entry"] == "Bash(scripts/install-reminder-hooks.sh:*)" for d in dropped
    )


def test_derivation_never_raises_on_refused_entry():
    # Same fixture as above via direct construction: must not raise.
    stage = _stage(
        executor="spawn:developer",
        output_artifacts=["scripts/install-reminder-hooks.sh"],
    )
    derive_stage_grants(stage, venue="/repo")  # no exception


# --- fixture-plan derivation: self-grants.toml and settings-channel-output.toml


def _write_self_grants_fixture(tmp_path: Path) -> Path:
    text = (FIXTURES / "self-grants.toml").read_text()
    text = text.replace("__CLAUDE_AGENT_HOME__", str(tmp_path / "agent-home"))
    out = tmp_path / "self-grants.toml"
    out.write_text(text)
    return out


def test_self_grants_fixture_derives_without_error(tmp_path):
    plan_path = _write_self_grants_fixture(tmp_path)
    doc = plan_mod.load_plan(str(plan_path), strict=False)
    venue = plan_mod._venue_for(doc)
    for stage in doc.stages:
        derive_stage_grants(stage, venue=venue)  # must not raise for any stage


def test_self_grants_fixture_declared_rules_present_verbatim():
    doc = plan_mod.load_plan(str(FIXTURES / "self-grants.toml"), strict=False)
    stage1 = next(s for s in doc.stages if s.index == 1)
    stage2 = next(s for s in doc.stages if s.index == 2)
    assert stage1.grants is not None
    assert "Bash(python3 scripts/probe-hook-decision-semantics.py:*)" in {
        g.rule for g in stage1.grants.allow
    }
    assert "Bash(git push origin perm-grants)" in {g.rule for g in stage1.grants.allow}
    assert stage2.grants is not None
    stage2_rules = {g.rule for g in stage2.grants.allow}
    assert "Bash(python3 scripts/replay-permission-guard.py:*)" in stage2_rules
    assert "WebFetch(domain:code.claude.com)" in stage2_rules
    add_dir_paths = {d.path for d in stage2.grants.add_dirs}
    assert any(p.endswith("/projects") for p in add_dir_paths)


def test_self_grants_fixture_dr_o_derives_probe_and_guard_artifacts(tmp_path):
    plan_path = _write_self_grants_fixture(tmp_path)
    doc = plan_mod.load_plan(str(plan_path), strict=False)
    venue = plan_mod._venue_for(doc)
    stage1 = next(s for s in doc.stages if s.index == 1)
    stage2 = next(s for s in doc.stages if s.index == 2)
    derived1, dropped1 = derive_stage_grants(stage1, venue=venue)
    derived2, dropped2 = derive_stage_grants(stage2, venue=venue)
    assert "Bash(python3 scripts/fixture_probe.py:*)" in _rules(derived1)
    assert "Bash(scripts/fixture_guard.sh:*)" in _rules(derived2)
    assert not dropped1
    assert not dropped2


def test_settings_channel_output_fixture_dr_o_dropped_not_allowed():
    doc = plan_mod.load_plan(
        str(FIXTURES / "settings-channel-output.toml"), strict=False
    )
    venue = plan_mod._venue_for(doc)
    stage = doc.stages[0]
    derived, dropped = derive_stage_grants(stage, venue=venue)
    assert "Bash(scripts/install-reminder-hooks.sh:*)" not in _rules(derived)
    assert any(
        d["entry"] == "Bash(scripts/install-reminder-hooks.sh:*)" for d in dropped
    )
