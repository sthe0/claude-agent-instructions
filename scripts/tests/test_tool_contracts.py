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
    assert table["sed"].effect == "resolver"
    assert table["dd"].effect == "unresolved"
    assert table["dd"].reason  # every declared-unresolved entry names why


def test_contract_table_rejects_duplicate_program_name(tmp_path):
    tc = _tool_contracts_module()

    dup = tmp_path / "dup.toml"
    dup.write_text(
        '[[program]]\nname = "ls"\neffect = "none"\nsafe_flags = ["*"]\n\n'
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


def test_sed_embedded_write_or_exec_command_is_unresolved(tmp_path):
    tc = _tool_contracts_module()

    target = tmp_path / "f.txt"
    target.write_text("hi")

    unresolved_scripts = [
        f"sed 'w out.txt' {target}",
        f"sed -n '3w out.txt' {target}",
        f"sed 's/a/b/w out.txt' {target}",
        f"sed '1,3e echo hi' {target}",
        f"sed 'e echo hi' {target}",
    ]
    for cmd in unresolved_scripts:
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "adhoc-undeclared", cmd

    # An 'e' occurring inside ordinary substitute TEXT (not as the command
    # letter) must not be mistaken for the exec command.
    letter_e_in_text = tc.resolve_command(f"sed 's/hi/bye/' {target}", str(tmp_path))
    assert letter_e_in_text.status == "resolved"
    assert letter_e_in_text.resources == []


def test_sed_closed_world_flag_and_block_parsing(tmp_path):
    """Finding #3, all 6 named cases from the review's root-cause finding:
    `-i` no longer short-circuits the w/W/e check (`-i` now goes through
    the SAME script parsing as every other invocation); an `s///e` exec
    flag counts as exec, not merely a non-`w` no-op; a `{...}` block is
    unresolved (its body is not recursively parsed, so it cannot be proven
    free of a nested w/W/e); and a bundled short flag (`-ne`) or a GNU
    long-option ABBREVIATION (`--expr=`, `--fil=`) is refused as outside
    the closed set rather than silently skipped."""
    tc = _tool_contracts_module()

    target = tmp_path / "f.txt"
    target.write_text("hi")

    unresolved_cmds = [
        f"sed -i '1e echo hi' {target}",
        f"sed -e 's/.*/id/e' {target}",
        f"sed '1{{e id;}}' {target}",
        f"sed -ne '1e id' {target}",
        f"sed --expr=1e {target}",
        f"sed --fil=script.sed {target}",
    ]
    for cmd in unresolved_cmds:
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class in ("contract-unresolved", "adhoc-undeclared"), cmd

    # Regression: ordinary flags in the reviewed closed set still resolve,
    # -i included -- this fix must not make -i itself unresolved, only stop
    # -i from bypassing the script check.
    for cmd in (
        f"sed -n -e p {target}",
        f"sed -i -e p {target}",
        f"sed -E -e p {target}",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "resolved", cmd


def test_find_exec_and_awk_are_unresolved(tmp_path):
    tc = _tool_contracts_module()

    exec_res = tc.resolve_command("find . -name '*.py' -exec rm {} \\;", str(tmp_path))
    assert exec_res.status == "unresolved"
    assert exec_res.reason_class == "contract-unresolved"

    plain_find = tc.resolve_command("find . -name '*.py'", str(tmp_path))
    assert plain_find.status == "resolved"
    assert plain_find.resources == []

    awk_res = tc.resolve_command("awk '{print > \"out.txt\"}' in.txt", str(tmp_path))
    assert awk_res.status == "unresolved"
    assert awk_res.reason_class == "contract-unresolved"


def test_find_delete_and_fprint_variants_are_unresolved(tmp_path):
    tc = _tool_contracts_module()

    for flag in ("-delete", "-fprint out.txt", "-fprint0 out.txt", "-fprintf out.txt %p", "-fls out.txt"):
        cmd = f"find . -name '*.py' {flag}"
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd


def test_process_substitution_is_unresolved(tmp_path):
    tc = _tool_contracts_module()

    for cmd in ("diff <(sort a.txt) <(sort b.txt)", "tee >(cat) < in.txt"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "residual-syntax", cmd


def test_git_output_flag_is_unresolved(tmp_path):
    tc = _tool_contracts_module()

    res = tc.resolve_command("git log --output=out.txt", str(tmp_path))
    assert res.status == "unresolved"
    assert res.reason_class == "contract-unresolved"


def test_git_venue_flags_require_matching_venue(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    other = tmp_path / "other"
    other.mkdir()

    for flag in ("-C", "--git-dir", "--work-tree"):
        cmd = f"git {flag} {other} push origin main"
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd

    same_dir = tc.resolve_command(f"git -C {tmp_path} push origin main", str(tmp_path))
    assert same_dir.status == "resolved"
    assert same_dir.resources == [resources.VcsRefResource("origin", "main", "push")]


def test_git_global_flags_outside_closed_set_are_unresolved(tmp_path):
    """Finding #1, root cause: only -C/--git-dir/--work-tree (already
    tested in test_git_venue_flags_require_matching_venue) and --no-pager
    are in the reviewed closed set of git GLOBAL flags. Every other global
    flag -- including ones a real `git` would happily accept -- is
    unresolved rather than silently skipped, since each can change WHICH
    resource a subsequent subcommand actually touches (`-c`/`--config-env`
    can rewrite the push destination via `url.insteadOf`; `--exec-path`
    points at a different git subprogram directory; `--namespace` targets
    a different ref namespace)."""
    tc = _tool_contracts_module()

    for cmd in (
        "git -c core.pager=cat status",
        "git --config-env=core.pager=cat status",
        "git --exec-path=/tmp status",
        "git --exec-path status",
        "git --namespace=foo status",
        "git --namespace foo status",
        "git --bogus-flag status",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd
        assert "outside the reviewed closed set" in (res.reason or ""), (cmd, res.reason)


def test_git_repeated_venue_flag_is_unresolved(tmp_path):
    """Finding #1: a repeated -C/--git-dir/--work-tree is refused rather
    than letting the second occurrence silently retarget git past the
    value the first occurrence was checked against."""
    tc = _tool_contracts_module()

    other = tmp_path / "other"
    other.mkdir()
    for cmd in (
        f"git -C {tmp_path} -C {other} status",
        f"git --git-dir={tmp_path} --git-dir={other} status",
        f"git --work-tree={tmp_path} --work-tree={other} status",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd
        assert "repeated" in (res.reason or ""), (cmd, res.reason)


def test_git_push_follow_tags_and_set_upstream_are_unresolved(tmp_path):
    """Finding #5: --follow-tags (pushes extra tag refs this table does not
    review) and -u/--set-upstream (writes local .git/config, a file this
    table does not resolve) are deliberately excluded from the allowed
    push-flag set -- each is unresolved rather than silently treated as a
    no-op push flag."""
    tc = _tool_contracts_module()

    for cmd in (
        "git push --follow-tags origin main",
        "git push -u origin main",
        "git push --set-upstream origin main",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd


def test_env_assignment_prefix_unresolved(tmp_path):
    """Finding #2, the shared root-cause primitive: a leading environment-
    variable assignment -- bare (`NAME=value cmd`) or via the `env`
    wrapper (`env NAME=value cmd`) -- is unresolved unless NAME is in the
    (today empty) reviewed-benign allowlist, since an injected variable
    can change a program's behavior in ways this table's own contract
    never reviewed."""
    tc = _tool_contracts_module()

    for cmd in (
        "GIT_SSH_COMMAND=evil git push origin main",
        "GIT_DIR=/tmp/other git status",
        "PYTHONPATH=/tmp/evil python3 -m pytest -q",
        "env GIT_SSH_COMMAND=evil git push origin main",
        "env PYTHONPATH=/tmp/evil python3 -m pytest -q",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd
        assert "environment assignment" in (res.reason or ""), (cmd, res.reason)


def test_rg_pre_and_date_set_are_unresolved(tmp_path):
    tc = _tool_contracts_module()

    rg_res = tc.resolve_command("rg --pre cat foo", str(tmp_path))
    assert rg_res.status == "unresolved"
    assert rg_res.reason_class == "contract-unresolved"

    rg_plain = tc.resolve_command("rg foo", str(tmp_path))
    assert rg_plain.status == "resolved"
    assert rg_plain.resources == []

    date_res = tc.resolve_command("date -s '2026-01-01'", str(tmp_path))
    assert date_res.status == "unresolved"
    assert date_res.reason_class == "contract-unresolved"

    date_plain = tc.resolve_command("date", str(tmp_path))
    assert date_plain.status == "resolved"
    assert date_plain.resources == []


def test_python_dash_c_is_unresolved_pytest_module_resolves_like_pytest(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    dash_c = tc.resolve_command("python3 -c 'print(1)'", str(tmp_path))
    assert dash_c.status == "unresolved"
    assert dash_c.reason_class == "adhoc-undeclared"

    via_module = tc.resolve_command("python3 -m pytest -q scripts/tests/test_foo.py", str(tmp_path))
    assert via_module.status == "resolved"
    assert via_module.resources == [resources.FileResource(str(tmp_path.resolve()), "write")]


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
        assert res.reason_class == "residual-syntax", cmd
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
        assert res.reason_class == "contract-unresolved"
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


def test_contract_resolver_closed_world_mutation(tmp_path):
    """Mutation-style discriminating test for the closed-world property.
    Two of the six mutation groups below are generated FROM THE LOADED
    CONTRACT TABLE itself (every `effect="none"` entry's own `safe_flags`,
    every wrapper name `_strip_closed_wrappers` actually recognizes) rather
    than hand-picked per-finding examples, so a future table entry sharing
    the same open-world gap fails this test even if nobody wrote a case
    naming it by hand. The remaining groups (env-assignment prefix, git
    global-flag grammar, and the always-refused wrapper-token set) have no
    table-side enumeration to drive from -- each is its own closed set by
    construction (see `_strip_closed_wrappers`'s docstring) -- so those stay
    literal, matching what the resolver's own source declares reviewed."""
    tc = _tool_contracts_module()
    resources = _resources_module()

    baseline_resolved_commands = [
        "ls -la",
        "git status",
        "git push origin main",
        "pytest -q scripts/tests/test_foo.py",
    ]

    # Mutation 1 (finding #2, root cause): an unreviewed env-assignment
    # prefix must unresolve EVERY baseline command, not only git/sed.
    for cmd in baseline_resolved_commands:
        baseline = tc.resolve_command(cmd, str(tmp_path))
        assert baseline.status == "resolved", cmd

        mutated = tc.resolve_command(f"UNREVIEWED_VAR=x {cmd}", str(tmp_path))
        assert mutated.status == "unresolved", cmd
        assert mutated.reason_class == "contract-unresolved", cmd

    # Mutation 2 (finding #1): an unrecognized git global flag must
    # unresolve a command that resolves cleanly without it.
    unknown_global_flag = tc.resolve_command("git --namespace=x status", str(tmp_path))
    assert unknown_global_flag.status == "unresolved"
    assert unknown_global_flag.reason_class == "contract-unresolved"

    # Mutation 3 (finding #1): a DUPLICATED venue-retargeting flag must
    # unresolve rather than let the second occurrence silently win.
    other = tmp_path / "other"
    other.mkdir()
    duplicated_venue_flag = tc.resolve_command(f"git -C {tmp_path} -C {other} status", str(tmp_path))
    assert duplicated_venue_flag.status == "unresolved"
    assert duplicated_venue_flag.reason_class == "contract-unresolved"
    assert "repeated" in (duplicated_venue_flag.reason or "")

    # Mutation 4 (N3, table-driven): an unrecognized flag on a bare
    # invocation of every `effect="none"` table entry must unresolve --
    # UNLESS that entry declares `safe_flags=["*"]`, the reviewed assertion
    # that no flag of that program writes anywhere, in which case the same
    # arbitrary flag is a POSITIVE control and must still resolve.
    table = tc.load_contract_table()
    unreviewed_flag = "--definitely-unreviewed-flag"
    for name, entry in table.items():
        if entry.effect != "none":
            continue
        if name == "env":
            # `env` is dispatched through `_strip_closed_wrappers` BEFORE
            # its own table entry is ever consulted -- any flag on it is
            # refused there (covered by mutation 5 below), so its
            # `safe_flags=["*"]` entry is a documented defensive backstop
            # this generic loop cannot reach and must not assert against.
            continue
        cmd = f"{name} {unreviewed_flag}"
        res = tc.resolve_command(cmd, str(tmp_path))
        if entry.safe_flags is not None and "*" in entry.safe_flags:
            assert res.status == "resolved", cmd
        else:
            assert res.status == "unresolved", cmd
            assert res.reason_class == "contract-unresolved", cmd
            assert "safe-flag" in (res.reason or ""), (cmd, res.reason)

    # Mutation 5 (N2, table-driven): every wrapper name `_strip_closed_
    # wrappers` recognizes (nice/timeout/nohup/env) must unresolve when
    # given a flag outside its own reviewed grammar, rather than silently
    # stepping past it.
    unreviewed_wrapper_flag_cmds = [
        f"nice {unreviewed_flag} ls -la",
        f"timeout {unreviewed_flag} 5 ls -la",
        f"nohup {unreviewed_flag} ls -la",
        f"env {unreviewed_flag} ls -la",
    ]
    for cmd in unreviewed_wrapper_flag_cmds:
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd

    # Mutation 6 (N2): a wrapper token outside the reviewed set (eval,
    # xargs, time, sudo, doas, flock -- none of which has a table entry of
    # its own) prefixed onto an otherwise-resolvable command must never
    # resolve; it falls through to the ordinary unknown-program refusal.
    for wrapper in ("eval", "xargs", "time", "sudo", "doas", "flock"):
        cmd = f"{wrapper} ls -la"
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "unknown-program", cmd

    # Mutation 7 (N1, non-destination operand coverage): every SOURCE
    # operand of a `mv`, not only its destination, must surface as its own
    # resolved write resource -- the piece the destination-only shared
    # write-target computation misses.
    src = tmp_path / "src.txt"
    src.write_text("x")
    dest = tmp_path / "dest.txt"
    mv_res = tc.resolve_command(f"mv {src} {dest}", str(tmp_path))
    assert mv_res.status == "resolved"
    assert resources.FileResource(str(src), "write") in mv_res.resources


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


# ---------------------------------------------------------------------------
# N1/N2/N3 dedicated per-case examples (round-3 review should-fix (d))
# ---------------------------------------------------------------------------


def test_mv_resolves_source_and_target_dir_and_refuses_unknown_flag(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    src = tmp_path / "src.txt"
    src.write_text("x")
    dest = tmp_path / "dest.txt"
    plain = tc.resolve_command(f"mv {src} {dest}", str(tmp_path))
    assert plain.status == "resolved"
    assert resources.FileResource(str(src), "write") in plain.resources
    assert resources.FileResource(str(dest), "write") in plain.resources

    other_src = tmp_path / "a.txt"
    other_src.write_text("x")
    dest_dir = tmp_path / "dir"
    dest_dir.mkdir()
    via_target_dir = tc.resolve_command(f"mv -t {dest_dir} {other_src}", str(tmp_path))
    assert via_target_dir.status == "resolved"
    assert resources.FileResource(str(other_src), "write") in via_target_dir.resources

    backup_flag = tc.resolve_command(f"mv -b {src} {dest}", str(tmp_path))
    assert backup_flag.status == "unresolved"
    assert backup_flag.reason_class == "contract-unresolved"


def test_patch_is_always_unresolved(tmp_path):
    tc = _tool_contracts_module()

    for cmd in ("patch < diff.patch", "patch -p1 -i diff.patch", "patch -o out.txt file.txt diff.patch"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd
        assert "patch" in (res.reason or "").lower(), cmd


def test_install_directory_mode_and_unknown_flag(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    newdir = tmp_path / "newdir"
    dir_mode = tc.resolve_command(f"install -d {newdir}", str(tmp_path))
    assert dir_mode.status == "resolved"
    assert resources.FileResource(str(newdir), "write") in dir_mode.resources

    src = tmp_path / "src.txt"
    src.write_text("x")
    dest = tmp_path / "dest.txt"
    plain = tc.resolve_command(f"install {src} {dest}", str(tmp_path))
    assert plain.status == "resolved"

    unknown_flag = tc.resolve_command(f"install -Z context {src} {dest}", str(tmp_path))
    assert unknown_flag.status == "unresolved"
    assert unknown_flag.reason_class == "contract-unresolved"


def test_cp_parents_is_unresolved(tmp_path):
    tc = _tool_contracts_module()

    src_dir = tmp_path / "a" / "b"
    src_dir.mkdir(parents=True)
    src = src_dir / "f.txt"
    src.write_text("x")
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    res = tc.resolve_command(f"cp --parents {src} {dest_dir}", str(tmp_path))
    assert res.status == "unresolved"
    assert res.reason_class == "contract-unresolved"
    assert "parents" in (res.reason or "")


def test_named_always_refused_wrapper_examples_are_unresolved(tmp_path):
    """N2, the exact examples named in the review finding: eval, xargs -a,
    time -o, sudo --chdir, and flock -c are never recognized as strippable
    wrappers -- each falls through to the ordinary unknown-program
    refusal, on the exact invocation shapes the finding named."""
    tc = _tool_contracts_module()

    for cmd in (
        "eval 'echo hi > out.txt'",
        "xargs -a list.txt rm",
        "time -o out.txt sleep 1",
        "sudo --chdir=/tmp tee out.txt",
        "flock -c 'echo hi > out.txt' lockfile",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "unknown-program", cmd


def test_env_chdir_is_unresolved(tmp_path):
    """N2: unlike the tokens above, `env` DOES have closed-wrapper handling
    (a leading run of bare assignments) -- but ANY flag on it, --chdir
    included, is refused explicitly rather than silently stepped past."""
    tc = _tool_contracts_module()

    res = tc.resolve_command("env --chdir=/tmp tee out.txt", str(tmp_path))
    assert res.status == "unresolved"
    assert res.reason_class == "contract-unresolved"
    assert "env flag" in (res.reason or "")


def test_nice_wrapped_write_resolves_on_stripped_segment(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    dest = tmp_path / "out.txt"
    res = tc.resolve_command(f"nice tee {dest}", str(tmp_path))
    assert res.status == "resolved"
    assert resources.FileResource(str(dest), "write") in res.resources


def test_nohup_wraps_and_contributes_nohup_out(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    res = tc.resolve_command("nohup ls -la", str(tmp_path))
    assert res.status == "resolved"
    assert resources.FileResource(str(tmp_path / "nohup.out"), "write") in res.resources


def test_none_effect_named_examples_outside_safe_flags_are_unresolved(tmp_path):
    """N3, the exact examples named in the review finding: `tree -o`,
    `less -o`, and `file -C` each has a write-capable flag deliberately
    excluded from its reviewed safe_flags allowlist."""
    tc = _tool_contracts_module()

    for cmd in ("tree -o out.txt", "less -o out.txt f.txt", "file -C f.txt"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd
        assert "safe-flag" in (res.reason or ""), cmd


def test_sed_bsd_ambiguous_empty_suffix_is_unresolved(tmp_path):
    """N4 (prior segment's fix): `sed -i ''` is ambiguous between GNU sed
    (an empty in-place suffix) and BSD/macOS sed (a mandatory, here-empty,
    suffix argument before the script) -- unresolved rather than guessing
    which reading applies, since guessing wrong risks missing an embedded
    w/W/e command hiding in whichever token is actually the script."""
    tc = _tool_contracts_module()

    target = tmp_path / "f.txt"
    target.write_text("hi")
    res = tc.resolve_command(f"sed -i '' 's/a/b/' {target}", str(tmp_path))
    assert res.status == "unresolved"
    assert res.reason_class == "contract-unresolved"
    assert "ambiguous" in (res.reason or "")
