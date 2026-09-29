"""Tests for the contract-grounded command resolver (scripts/agentctl/
tool_contracts.py + tool_contracts.toml).

Every test asserts `importlib.util.find_spec` presence for the module it
exercises BEFORE importing it, matching test_resource_model.py's convention,
so a test fails by ASSERTION (not ImportError) on a tree predating this
stage — the shape `nc.sh` requires.
"""
from __future__ import annotations

import importlib.util

import pytest


def _tool_contracts_module():
    spec = importlib.util.find_spec("agentctl.tool_contracts")
    assert spec is not None, "agentctl.tool_contracts module not found"
    from agentctl import tool_contracts

    return tool_contracts


def _resources_module():
    spec = importlib.util.find_spec("agentctl.resources")
    assert spec is not None, "agentctl.resources module not found"
    from agentctl import resources

    return resources


def test_contract_table_loads_one_entry_per_program():
    tc = _tool_contracts_module()

    table = tc.load_contract_table()
    assert "git" in table
    assert table["git"].effect == "resolver"
    assert table["ls"].effect == "none"
    assert table["sed"].effect == "writes-operands"
    assert table["dd"].effect == "unresolved"
    assert table["dd"].reason  # every declared-unresolved entry names why


def test_contract_table_rejects_duplicate_program_name(tmp_path):
    tc = _tool_contracts_module()

    dup = tmp_path / "dup.toml"
    dup.write_text(
        '[[program]]\nname = "ls"\neffect = "none"\n\n'
        '[[program]]\nname = "ls"\neffect = "unresolved"\nreason = "x"\n'
    )
    with pytest.raises(ValueError, match="duplicate"):
        tc.load_contract_table(dup)


def test_readonly_commands_resolve_with_no_resource(tmp_path):
    tc = _tool_contracts_module()

    for cmd in ("ls -la", "cat foo.txt", "grep -n x foo.txt", "git status"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "resolved", cmd
        assert res.resources == [], cmd


def test_sed_in_place_writes_file_plain_sed_does_not(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    target = tmp_path / "f.txt"
    target.write_text("hi")

    # bash_write_targets is deliberately over-inclusive for `sed -i`: every
    # non-flag token in the segment (the sed script AND the file operand)
    # becomes a candidate write target, since which token is "the script"
    # vs "the file" is not reliably decidable from shape alone. The real
    # file operand is among the candidates, which is what matters here.
    in_place = tc.resolve_command(f"sed -i s/a/b/ {target}", str(tmp_path))
    assert in_place.status == "resolved"
    assert resources.FileResource(str(target), "write") in in_place.resources

    plain = tc.resolve_command(f"sed s/a/b/ {target}", str(tmp_path))
    assert plain.status == "resolved"
    assert plain.resources == []


def test_find_exec_and_awk_are_unresolved(tmp_path):
    tc = _tool_contracts_module()

    exec_res = tc.resolve_command("find . -name '*.py' -exec rm {} \\;", str(tmp_path))
    assert exec_res.status == "unresolved"
    assert exec_res.reason_class == "declared-unresolved"

    plain_find = tc.resolve_command("find . -name '*.py'", str(tmp_path))
    assert plain_find.status == "resolved"
    assert plain_find.resources == []

    awk_res = tc.resolve_command("awk '{print > \"out.txt\"}' in.txt", str(tmp_path))
    assert awk_res.status == "unresolved"
    assert awk_res.reason_class == "declared-unresolved"


def test_redirect_and_cp_targets_become_file_resources(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    dest = tmp_path / "out.txt"
    redirect = tc.resolve_command(f"echo hi > {dest}", str(tmp_path))
    assert redirect.status == "resolved"
    assert redirect.resources == [resources.FileResource(str(dest), "write")]

    src = tmp_path / "src.txt"
    src.write_text("x")
    dest_dir = tmp_path / "dir"
    dest_dir.mkdir()
    cp_res = tc.resolve_command(f"cp {src} {dest_dir}/", str(tmp_path))
    assert cp_res.status == "resolved"
    assert cp_res.resources == [resources.FileResource(str(dest_dir / "src.txt"), "write")]


def test_dollar_backtick_and_subshell_segments_unresolved(tmp_path):
    tc = _tool_contracts_module()

    for cmd in ("echo $(rm -rf /tmp/x)", "echo `date`", "(cd /tmp && rm -rf x)"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "nested-execution", cmd
        assert res.identity is not None, cmd


def test_pytest_resolves_to_venue_subtree(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    res = tc.resolve_command("pytest -q scripts/tests/test_foo.py", str(tmp_path))
    assert res.status == "resolved"
    assert res.resources == [resources.FileResource(str(tmp_path.resolve()), "write")]


def test_undeclared_adhoc_script_is_unresolved_with_identity(tmp_path):
    tc = _tool_contracts_module()

    outside = tmp_path.parent / f"helper-{tmp_path.name}.py"
    outside.write_text("print('hi')\n")
    try:
        res = tc.resolve_command(f"python3 {outside}", str(tmp_path))
        assert res.status == "unresolved"
        assert res.reason_class == "declared-unresolved"
        assert res.identity is not None
        assert res.identity[0] == "unresolved"
    finally:
        outside.unlink()


def test_missing_operand_encoded_absent_changes_identity_when_created(tmp_path):
    tc = _tool_contracts_module()

    outside = tmp_path.parent / f"missing-{tmp_path.name}.txt"
    if outside.exists():
        outside.unlink()
    try:
        before = tc.resolve_command(f"unknownprogramxyz {outside}", str(tmp_path))
        assert before.status == "unresolved"
        assert before.reason_class == "unknown-program"
        assert ("absent" in str(before.identity))

        outside.write_text("now exists")
        after = tc.resolve_command(f"unknownprogramxyz {outside}", str(tmp_path))
        assert after.identity != before.identity
    finally:
        if outside.exists():
            outside.unlink()


def test_sibling_helper_edit_changes_unresolved_script_identity(tmp_path):
    tc = _tool_contracts_module()

    outer = tmp_path.parent / f"scriptdir-{tmp_path.name}"
    outer.mkdir()
    script = outer / "main.py"
    script.write_text("import helper\n")
    sibling = outer / "helper.py"
    sibling.write_text("VALUE = 1\n")
    try:
        before = tc.resolve_command(f"python3 {script}", str(tmp_path))
        sibling.write_text("VALUE = 2\n")
        after = tc.resolve_command(f"python3 {script}", str(tmp_path))
        assert before.identity != after.identity
    finally:
        sibling.unlink()
        script.unlink()
        outer.rmdir()


def test_force_and_delete_push_not_covered_by_push(tmp_path):
    """git push variants that are NOT a plain literal-refspec push: force,
    delete, and — per the root's explicit continuation constraint #1 — a
    ZERO-refspec push (`git push` / `git push origin` with no refspec),
    whose real target ref depends on git config, not on the command line."""
    tc = _tool_contracts_module()
    resources = _resources_module()

    plain = tc.resolve_command("git push origin main", str(tmp_path))
    assert plain.status == "resolved"
    assert plain.resources == [resources.VcsRefResource("origin", "main", "push")]

    refusals = [
        "git push --force origin main",
        "git push -f origin main",
        "git push --force-with-lease origin main",
        "git push --delete origin main",
        "git push origin :main",
        "git push",
        "git push origin",
    ]
    for cmd in refusals:
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class in ("force-or-delete-push", "contract-unresolved"), cmd
        # An agent self-grant must never treat any of these as covered by a
        # plain-push approval on the same ref.
        approved_plain_push = resources.VcsRefResource("origin", "main", "push")
        for r in res.resources:
            assert not approved_plain_push.covers(r)


def test_land_op_never_produced_by_this_resolver(tmp_path):
    """C1: no contract entry or custom resolver in this module may ever
    return a VcsRefResource with op='land' — landing is decided solely by
    the (separately checkpointed) landed-spec resolver."""
    tc = _tool_contracts_module()
    resources = _resources_module()

    commands = [
        "git push origin main",
        "git status",
        "ls -la",
        "sed -i s/a/b/ f.txt",
        "cp a b",
        "pytest -q",
    ]
    for cmd in commands:
        res = tc.resolve_command(cmd, str(tmp_path))
        for r in res.resources:
            if isinstance(r, resources.VcsRefResource):
                assert r.op != "land", cmd


def test_contract_table_and_ledger_never_covered_via_resolver(tmp_path):
    """CLI-integration-adjacent form of test_contract_tables_and_ledger_dir_
    never_covered: resolving a command that plainly writes the contract
    table itself still yields a resource resources.py's own protected-
    surfaces logic refuses to cover — the resolver does not special-case
    the path, it stays an ordinary FileResource and the refusal lives in
    resources.py (already tested there)."""
    tc = _tool_contracts_module()
    resources = _resources_module()

    venue = tmp_path / "venue"
    (venue / "scripts" / "agentctl").mkdir(parents=True)
    contracts_path = venue / "scripts" / "agentctl" / "tool_contracts.toml"
    contracts_path.write_text("")

    res = tc.resolve_command(f"echo x > {contracts_path}", str(venue))
    assert res.status == "resolved"
    assert res.resources == [resources.FileResource(str(contracts_path), "write")]

    protected = resources.protected_permission_surfaces(repo_root=str(venue))
    approved_whole_venue = resources.FileResource(str(venue), "write")
    assert not approved_whole_venue.covers(res.resources[0], protected=protected)
