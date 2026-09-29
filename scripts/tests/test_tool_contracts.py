"""Tests for the contract-grounded command resolver (scripts/agentctl/
tool_contracts.py + tool_contracts.toml).

Every test asserts `importlib.util.find_spec` presence for the module it
exercises BEFORE importing it, matching test_resource_model.py's convention,
so a test fails by ASSERTION (not ImportError) on a tree predating this
stage — the shape `nc.sh` requires.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

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


def _land_branch_registry_venue(tmp_path):
    """A minimal venue tree with `scripts/land-branch.py` copied verbatim
    from the real repo script, so its sha256 matches the entry already
    pinned in the REPO-GLOBAL `scripts/script_effects.toml` --
    `_resolve_interpreter` consults that global table (not a venue-local
    one), so a script only resolves through the registry when its bytes are
    byte-identical to the reviewed copy. Mirrors test_resource_model.py's
    identically-named helper, duplicated here to keep this file's own
    imports self-contained."""
    venue = tmp_path / "venue"
    (venue / "scripts").mkdir(parents=True)
    (venue / ".git").mkdir()
    real_land_branch = Path(__file__).resolve().parent.parent / "land-branch.py"
    (venue / "scripts" / "land-branch.py").write_text(
        real_land_branch.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return venue


def test_contract_table_loads_one_entry_per_program():
    tc = _tool_contracts_module()

    table = tc.load_contract_table()
    assert "git" in table
    assert table["git"].effect == "resolver"
    assert table["ls"].effect == "none"
    assert table["rg"].effect == "none"
    assert table["sed"].effect == "unresolved"
    assert table["cp"].effect == "unresolved"
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


def test_rg_safe_flags_closed_set(tmp_path):
    """rg's own closed flag set has no write flag in it; --pre (runs an
    arbitrary preprocessor command per searched file) and --replace/-r are
    deliberately excluded, so either turns the whole command unresolved."""
    tc = _tool_contracts_module()

    plain = tc.resolve_command("rg foo", str(tmp_path))
    assert plain.status == "resolved"
    assert plain.resources == []

    for cmd in ("rg --pre cat foo", "rg -r replacement foo"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd


def test_date_is_always_unresolved(tmp_path):
    """date is outside the reviewed read-only program table entirely, and
    -s/--set changes system clock state -- unresolved regardless of args,
    including a bare invocation with no flags at all."""
    tc = _tool_contracts_module()

    for cmd in ("date", "date -s '2026-01-01'"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd


def test_sed_is_always_unresolved(tmp_path):
    """Deciding sed's actual effect requires knowing both whether -i is
    present and whether its own script text hides an embedded w/W/e
    command -- neither is in the reviewed closed command grammar, so every
    sed invocation is unresolved, the in-place, plain-stdout, and
    ambiguous-suffix forms alike."""
    tc = _tool_contracts_module()

    target = tmp_path / "f.txt"
    target.write_text("hi")
    for cmd in (
        f"sed -i s/a/b/ {target}",
        f"sed s/a/b/ {target}",
        f"sed -i '' 's/a/b/' {target}",
        f"sed -e '1e id' {target}",
        f"sed -n -e p {target}",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd


def test_removed_write_target_verbs_are_always_unresolved(tmp_path):
    """cp, mv, install, tee, awk, and find each used to have their own
    write-target or argument parsing; none of that parsing is in the
    reviewed closed command grammar any more, so every invocation of any of
    them is unresolved regardless of flags."""
    tc = _tool_contracts_module()

    src = tmp_path / "src.txt"
    src.write_text("x")
    dest_dir = tmp_path / "dir"
    dest_dir.mkdir()

    for cmd in (
        f"cp {src} {dest_dir}/",
        f"cp --parents {src} {dest_dir}",
        f"mv {src} {dest_dir}/dest.txt",
        f"install {src} {dest_dir}/dest.txt",
        f"install -d {dest_dir}/newdir",
        f"tee {dest_dir}/out.txt",
        "awk -f script.awk in.txt",
        "find . -name test.py",
        "find . -type f -name test.py",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd


def test_patch_is_always_unresolved(tmp_path):
    tc = _tool_contracts_module()

    for cmd in ("patch -p1 -i diff.patch", "patch -o out.txt file.txt diff.patch"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd
        assert "patch" in (res.reason or "").lower(), cmd

    # A redirect-fed form (`patch < diff.patch`) is unresolved for the
    # command-shape reason (item (a)'s redirect prohibition), not patch's
    # own program-effect reason -- the shape check runs first and refuses
    # the whole command before any per-program dispatch is reached.
    redirected = tc.resolve_command("patch < diff.patch", str(tmp_path))
    assert redirected.status == "unresolved"
    assert redirected.reason_class == "contract-unresolved"
    assert "redirection" in (redirected.reason or "").lower()


def test_wrapper_prefixed_commands_are_always_unresolved(tmp_path):
    """Every wrapper token in the reviewed closed command grammar's refusal
    set (env, nice, timeout, nohup, eval, xargs, sudo, time, flock, command,
    exec, builtin) is refused wherever it leads a segment -- there is no
    stripped-wrapper form any more, since a wrapper can change what actually
    runs, or how, in ways its own token does not show."""
    tc = _tool_contracts_module()

    target = tmp_path / "out.txt"
    for cmd in (
        f"env tee {target}",
        f"nice tee {target}",
        f"timeout 5 tee {target}",
        "nohup ls -la",
        f"eval 'echo hi > {target}'",
        "xargs -a list.txt rm",
        f"sudo --chdir=/tmp tee {target}",
        "time -o out.txt sleep 1",
        f"flock -c 'echo hi > {target}' lockfile",
        "command ls -la",
        "exec ls -la",
        "builtin cd /tmp",
    ):
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
    """Only -C/--git-dir/--work-tree (tested separately) and --no-pager are
    in the reviewed closed set of git GLOBAL flags. Every other global flag
    -- including ones a real `git` would happily accept -- is unresolved
    rather than silently skipped, since each can change WHICH resource a
    subsequent subcommand actually touches (`-c`/`--config-env` can rewrite
    the push destination via `url.insteadOf`; `--exec-path` points at a
    different git subprogram directory; `--namespace` targets a different
    ref namespace)."""
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
    """A repeated -C/--git-dir/--work-tree is refused rather than letting
    the second occurrence silently retarget git past the value the first
    occurrence was checked against."""
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
    """--follow-tags (pushes extra tag refs this table does not review) and
    -u/--set-upstream (writes local .git/config, a file this table does not
    resolve) are deliberately excluded from the allowed push-flag set --
    each is unresolved rather than silently treated as a no-op push flag."""
    tc = _tool_contracts_module()

    for cmd in (
        "git push --follow-tags origin main",
        "git push -u origin main",
        "git push --set-upstream origin main",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd


def test_git_branch_safe_flags_resolve_others_unresolved(tmp_path):
    """git branch resolves to no effect under its own closed boolean-flag
    set; any value-taking flag or positional branch name falls outside that
    set and is unresolved, since each names a create/rename/delete or a
    different filter than plain listing."""
    tc = _tool_contracts_module()

    for cmd in ("git branch", "git branch -a", "git branch --list", "git branch -v"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "resolved", cmd
        assert res.resources == [], cmd

    for cmd in ("git branch new-feature", "git branch -d old-feature", "git branch --contains HEAD"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd


def test_git_readonly_subcommand_flags_are_closed_per_subcommand(tmp_path):
    """B2: each read-only subcommand (log/diff/show/rev-parse/ls-files/
    blame) reviews its OWN closed boolean-flag set (mirroring
    `_GIT_BRANCH_SAFE_FLAGS`'s pattern), not just a shared `--output=`
    check. A flag outside that subcommand's set -- including one this
    table never reviewed at all (`--ext-diff`/`--textconv`, each able to
    invoke an external program via `diff.external`/a configured filter
    driver; `--show-signature`, which invokes `gpg`) and an abbreviated
    form of a flag this table DOES review (`--outpu=x`, valid under git's
    own unique-prefix long-option abbreviation) -- must be unresolved, not
    silently passed through to "no effect". A bare invocation and one using
    only that subcommand's own reviewed flags must still resolve."""
    tc = _tool_contracts_module()

    for subcommand, safe_flag in (
        ("log", "--oneline"),
        ("diff", "--stat"),
        ("show", "--stat"),
        ("rev-parse", "--short"),
        ("ls-files", "--cached"),
        ("blame", "-w"),
    ):
        bare = tc.resolve_command(f"git {subcommand}", str(tmp_path))
        assert bare.status == "resolved", subcommand
        assert bare.resources == [], subcommand

        with_safe_flag = tc.resolve_command(f"git {subcommand} {safe_flag}", str(tmp_path))
        assert with_safe_flag.status == "resolved", subcommand

        for unsafe in ("--ext-diff", "--textconv", "--show-signature", "--outpu=x"):
            res = tc.resolve_command(f"git {subcommand} {unsafe}", str(tmp_path))
            assert res.status == "unresolved", (subcommand, unsafe)
            assert res.reason_class == "contract-unresolved", (subcommand, unsafe)


def test_git_push_tag_shorthand_is_unresolved(tmp_path):
    """`git push <remote> tag <name>` is git's own distinct shorthand for
    pushing `refs/tags/<name>` -- not two independent refspecs named "tag"
    and "<name>". A flat refspec-list resolver that doesn't know this
    grammar would otherwise report a nonsensical first destination named
    literally "tag"; this must be unresolved instead. A single positional
    refspec that happens to be literally named "tag" (no name follows it)
    is unaffected -- there is no shorthand to misread there."""
    tc = _tool_contracts_module()

    res = tc.resolve_command("git push origin tag v1", str(tmp_path))
    assert res.status == "unresolved"
    assert res.reason_class == "contract-unresolved"

    literal_tag_ref = tc.resolve_command("git push origin tag", str(tmp_path))
    assert literal_tag_ref.status == "resolved"


def test_env_assignment_prefix_unresolved(tmp_path):
    """A leading NAME=value assignment disqualifies its segment outright:
    an injected variable can change any program's behavior in ways this
    table cannot see (a different config file, a different PATH, an
    injected interpreter flag via a *_OPTS-style variable)."""
    tc = _tool_contracts_module()

    for cmd in (
        "GIT_SSH_COMMAND=evil git push origin main",
        "GIT_DIR=/tmp/other git status",
        "PYTHONPATH=/tmp/evil python3 -m pytest -q",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd
        assert "environment assignment" in (res.reason or ""), (cmd, res.reason)

    # `env` is itself a forbidden wrapper token (its own dedicated test), so
    # `env NAME=value cmd` is refused there first -- the wrapper-token check
    # runs before this module ever looks at env's own operands.
    for cmd in (
        "env GIT_SSH_COMMAND=evil git push origin main",
        "env PYTHONPATH=/tmp/evil python3 -m pytest -q",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd


def test_python_dash_c_is_unresolved_pytest_module_resolves_like_pytest(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    dash_c = tc.resolve_command("python3 -c 'print(1)'", str(tmp_path))
    assert dash_c.status == "unresolved"
    assert dash_c.reason_class == "adhoc-undeclared"

    via_module = tc.resolve_command("python3 -m pytest -q scripts/tests/test_foo.py", str(tmp_path))
    assert via_module.status == "resolved"
    assert via_module.resources == [resources.FileResource(str(tmp_path.resolve()), "write")]


def test_interpreter_dash_flags_outside_closed_set_are_unresolved(tmp_path):
    """Only exact `-c` and `-m` (each space-separated, never glued or
    `=`-joined) are recognized as interpreter flags before the script; a
    glued short form (`-mMOD`, `-cCODE`), CPython's nonexistent `-m=`/
    `--command`/`--command=` spellings, or any other dash-prefixed flag
    (`-W`, `-X`, `-u`) must be refused outright, never silently skipped
    past to reach a later token as "the script" or "the -m module".
    Regression: a prior version's flag loop fell through silently on an
    unrecognized dash token, so `-mMOD scripts/land-branch.py --check` was
    mistaken for a plain script invocation (resolving via the
    script-effects registry) and `-cCODE -m pytest` was mistaken for a
    plain `-m pytest` run -- when the ACTUAL python3 semantics run an
    arbitrary module / arbitrary inline code instead. A separate prior
    version also treated `-m=MOD` as equivalent to `-m MOD`, when CPython's
    own glued short-option parsing actually reads the module name as the
    literal string `=MOD`, not `MOD` -- so `-m=pytest` does NOT run pytest
    at all and must not be special-cased to pytest's whole-venue-write
    verdict. The venue below carries a real, registry-matching
    `land-branch.py` so the OLD code's false "resolved" is actually
    reachable, not masked by an unrelated "script not found"."""
    tc = _tool_contracts_module()
    venue = _land_branch_registry_venue(tmp_path)

    for cmd in (
        "python3 -mMOD scripts/land-branch.py --check",
        "python3 -cCODE -m pytest",
        "python3 -W ignore scripts/land-branch.py --check",
        "python3 -X utf8 -m pytest",
        "python3 -u scripts/land-branch.py --check",
        "python3 -m=pytest -q",
        "python3 --command x",
        "python3 --command=x",
    ):
        res = tc.resolve_command(cmd, str(venue))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd


def test_land_branch_dash_c_resolved_against_venue_not_engine_cwd(tmp_path, monkeypatch):
    """B3: land-branch.py's `-C` value must be judged against the VENUE --
    the resource this resolution decides whether to self-grant a push for
    -- never against whatever directory the analyzing (agentctl engine)
    process happens to be running from. Regression: the old code called
    bare `os.path.realpath(os.path.expanduser(dash_c))` on a RELATIVE `-C`
    value with no base at all, so it silently resolved against the
    ENGINE's own cwd. Rigged here so a relative `-C ..`, computed from the
    engine's (monkeypatched) cwd one level inside the venue, coincidentally
    lands back on the venue itself -- even though the textually-identical
    command, run for real with the venue as ITS working directory, targets
    the venue's PARENT: a different checkout entirely."""
    tc = _tool_contracts_module()
    venue = _land_branch_registry_venue(tmp_path)
    venue_real = str(venue.resolve())

    engine_cwd = venue / "somedir"
    engine_cwd.mkdir()
    monkeypatch.chdir(engine_cwd)

    r = tc.resolve_command(
        "python3 scripts/land-branch.py -C .. --branch feature --keep-branch", venue_real
    )
    assert r.status == "unresolved", r
    assert r.reason_class == "contract-unresolved", r
    assert "different checkout" in (r.reason or ""), r.reason


def test_pytest_resolves_to_venue_subtree(tmp_path):
    tc = _tool_contracts_module()
    resources = _resources_module()

    res = tc.resolve_command("pytest -q scripts/tests/test_foo.py", str(tmp_path))
    assert res.status == "resolved"
    assert res.resources == [resources.FileResource(str(tmp_path.resolve()), "write")]


def test_pytest_closed_flag_and_path_grammar(tmp_path):
    """pytest's own closed flag/path grammar (item (c)): a flag outside the
    reviewed set, an absolute test path, and a test path escaping the venue
    via '..' each turn the whole command unresolved rather than partially
    trusting the rest of the line."""
    tc = _tool_contracts_module()

    for cmd in (
        "pytest --maxfail=1",
        "pytest -p someplugin",
        "pytest --tb=long",
        f"pytest {tmp_path}/scripts/tests/test_foo.py",
        "pytest ../outside/test_foo.py",
    ):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class in ("contract-unresolved", "residual-syntax"), cmd


def test_closed_grammar_resolved_examples(tmp_path):
    """Three commands squarely inside the closed grammar's positive cases:
    pytest with its full reviewed flag set, a chained pair of read-only git
    subcommands, and pytest piped through tail with the one reviewed
    redirection exception (`2>&1`, stderr merged into stdout)."""
    tc = _tool_contracts_module()
    resources = _resources_module()

    pytest_res = tc.resolve_command(
        "pytest -q -x -v -s -k expr -p no:cacheprovider --tb=short scripts/tests/test_foo.py",
        str(tmp_path),
    )
    assert pytest_res.status == "resolved"
    assert pytest_res.resources == [resources.FileResource(str(tmp_path.resolve()), "write")]

    git_chain = tc.resolve_command("git status && git diff", str(tmp_path))
    assert git_chain.status == "resolved"
    assert git_chain.resources == []

    piped = tc.resolve_command("pytest -q 2>&1 | tail -5", str(tmp_path))
    assert piped.status == "resolved"
    assert piped.resources == [resources.FileResource(str(tmp_path.resolve()), "write")]


def test_redirection_operators_are_unresolved_except_stderr_merge(tmp_path):
    """Every redirection operator disqualifies its whole command except the
    one reviewed exception, the standalone token `2>&1` (stderr merged into
    stdout) -- a redirect target is an arbitrary path this table has no
    write-target parser for any more."""
    tc = _tool_contracts_module()
    resources = _resources_module()

    dest = tmp_path / "out.txt"
    for op in (">", ">>", ">|", "<", "<<", "<<<"):
        cmd = f"echo hi {op} {dest}"
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd

    merged = tc.resolve_command("pytest -q 2>&1", str(tmp_path))
    assert merged.status == "resolved"
    assert merged.resources == [resources.FileResource(str(tmp_path.resolve()), "write")]


def test_dollar_backtick_and_subshell_segments_unresolved(tmp_path):
    tc = _tool_contracts_module()

    for cmd in ("echo $(rm -rf /tmp/x)", "echo `date`", "(cd /tmp && rm -rf x)"):
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "residual-syntax", cmd
        assert res.identity is not None, cmd


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


def test_land_op_never_produced_by_this_resolver(tmp_path):
    """No contract entry or custom resolver in this module may ever return
    a VcsRefResource with op='land' -- landing is decided solely by the
    (separately checkpointed) landed-spec resolver."""
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


def test_closed_world_mutation_catalogue(tmp_path):
    """One table-driven negative control per clause of the closed command
    grammar (`_check_command_shape`) and per closed per-program flag set in
    the loaded contract table: each mutation below takes a command this
    resolver accepts and applies exactly one violation named in the closed
    grammar, and every mutated command must turn unresolved -- proving each
    clause is load-bearing rather than a dead check nothing exercises."""
    tc = _tool_contracts_module()

    resolved_examples = [
        "ls -la",
        "git status",
        "git push origin main",
        "pytest -q scripts/tests/test_foo.py",
    ]
    for cmd in resolved_examples:
        baseline = tc.resolve_command(cmd, str(tmp_path))
        assert baseline.status == "resolved", cmd

    # (v) a leading environment-variable assignment.
    for cmd in resolved_examples:
        mutated = tc.resolve_command(f"UNREVIEWED_VAR=x {cmd}", str(tmp_path))
        assert mutated.status == "unresolved", cmd

    # (vi) each wrapper token, as a prefix.
    wrapper_tokens = (
        "env", "nice", "timeout", "nohup", "eval", "xargs", "sudo",
        "time", "flock", "command", "exec", "builtin",
    )
    for wrapper in wrapper_tokens:
        for cmd in resolved_examples:
            mutated = tc.resolve_command(f"{wrapper} {cmd}", str(tmp_path))
            assert mutated.status == "unresolved", (wrapper, cmd)

    # (ii) a path-qualified program token.
    for cmd in resolved_examples:
        prog, _, rest = cmd.partition(" ")
        mutated_cmd = f"./{prog} {rest}" if rest else f"./{prog}"
        mutated = tc.resolve_command(mutated_cmd, str(tmp_path))
        assert mutated.status == "unresolved", mutated_cmd

    # (iii) each redirection operator, outside the one reviewed exception.
    for op in (">", ">>", ">|", ">&", "&>", "<", "<<", "<<<"):
        for cmd in resolved_examples:
            mutated = tc.resolve_command(f"{cmd} {op} x", str(tmp_path))
            assert mutated.status == "unresolved", (op, cmd)

    # (iv) each non-literal marker, plus a leading '~', appended as a
    # trailing operand.
    for marker_token in ("$HOME", "`pwd`", "*.txt", "file?.txt", "[abc].txt", "{a,b}.txt", "~/file.txt"):
        for cmd in resolved_examples:
            mutated = tc.resolve_command(f"{cmd} {marker_token}", str(tmp_path))
            assert mutated.status == "unresolved", (marker_token, cmd)

    # (i) an unknown flag, generated from the loaded contract table itself:
    # every effect="none" entry either declares safe_flags=["*"] (any flag
    # is a positive control and must still resolve) or a closed set (an
    # unreviewed flag must unresolve).
    table = tc.load_contract_table()
    for name, entry in table.items():
        if entry.effect != "none":
            continue
        cmd = f"{name} --definitely-unreviewed-flag"
        res = tc.resolve_command(cmd, str(tmp_path))
        if entry.safe_flags is not None and "*" in entry.safe_flags:
            assert res.status == "resolved", cmd
        else:
            assert res.status == "unresolved", cmd
            assert res.reason_class == "contract-unresolved", cmd

    # (B1) each interpreter flag before the script that is outside the
    # closed -c/--command/-m set: a glued short form and an unreviewed
    # value-taking flag alike.
    for flag in ("-cCODE", "-mMOD", "-W", "-X", "-u", "-O"):
        cmd = f"python3 {flag} scripts/does_not_matter.py"
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd

    # (B2) each read-only git subcommand with a flag outside its own closed
    # set -- generated from `_GIT_READONLY_SAFE_FLAGS` itself so a future
    # subcommand added to that table is covered automatically, rather than
    # from a hand-copied list that silently stops tracking it.
    for subcommand in tc._GIT_READONLY_SAFE_FLAGS:
        cmd = f"git {subcommand} --ext-diff"
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd

    # (B2) `git push` with a flag outside `_GIT_PUSH_ALLOWED_FLAGS`.
    for flag in ("--force-with-lease", "--receive-pack=evil", "--signed"):
        cmd = f"git push {flag} origin main"
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd

    # (B3) land-branch.py's -C with a relative base that does NOT resolve
    # to the venue: covered end-to-end (registry lookup + digest match,
    # false-positive-fooled-engine-cwd) by
    # test_land_branch_dash_c_resolved_against_venue_not_engine_cwd; here we
    # only confirm a plain relative-but-wrong -C is still refused through
    # the same reviewed venue-relative path resolution.
    venue = _land_branch_registry_venue(tmp_path)
    wrong_base = tc.resolve_command(
        "python3 scripts/land-branch.py -C .. --branch feature --keep-branch", str(venue)
    )
    assert wrong_base.status == "unresolved"

    # (F1) an '@'-prefixed pytest operand -- pytest >= 8.2's own @argsfile
    # expansion, both as a bare test-path positional and as the value of
    # '-k'.
    at_positional = tc.resolve_command("pytest @evil.args", str(tmp_path))
    assert at_positional.status == "unresolved"
    assert at_positional.reason_class == "contract-unresolved"

    at_k_value = tc.resolve_command("pytest -k @evil.args", str(tmp_path))
    assert at_k_value.status == "unresolved"
    assert at_k_value.reason_class == "contract-unresolved"

    # every effect="unresolved" table entry must refuse regardless of the
    # operand it is given -- it is permanently unresolved by declaration,
    # not merely absent a reviewed flag.
    table = tc.load_contract_table()
    for name, entry in table.items():
        if entry.effect != "unresolved":
            continue
        cmd = f"{name} --whatever-operand-this-is"
        res = tc.resolve_command(cmd, str(tmp_path))
        assert res.status == "unresolved", cmd
        assert res.reason_class == "contract-unresolved", cmd

    # (vii) each segment operator outside the closed '&&'/';'/'|' set.
    for op in ("||", "|&", "&"):
        for cmd in resolved_examples:
            mutated = tc.resolve_command(f"{cmd} {op} {cmd}", str(tmp_path))
            assert mutated.status == "unresolved", (op, cmd)
            assert mutated.reason_class == "contract-unresolved", (op, cmd)

    # (viii) an empty command segment -- a leading, trailing, or doubled
    # separator.
    for cmd in resolved_examples:
        for mutated_cmd in (f"; {cmd}", f"{cmd} ;", f"{cmd} ;; {cmd}"):
            mutated = tc.resolve_command(mutated_cmd, str(tmp_path))
            assert mutated.status == "unresolved", mutated_cmd
            assert mutated.reason_class == "contract-unresolved", mutated_cmd

    # (ix) a bare subshell, unattached to any redirect/operator.
    for cmd in resolved_examples:
        mutated = tc.resolve_command(f"({cmd})", str(tmp_path))
        assert mutated.status == "unresolved", cmd
        assert mutated.reason_class == "residual-syntax", cmd

    # (x) a bare newline statement separator smuggling in a second,
    # unreviewed statement.
    for cmd in resolved_examples:
        mutated = tc.resolve_command(f"{cmd}\nrm -rf x", str(tmp_path))
        assert mutated.status == "unresolved", cmd

    # (xi) a '#' comment marker leading its own segment after a newline.
    for cmd in resolved_examples:
        mutated = tc.resolve_command(f"{cmd}\n#comment", str(tmp_path))
        assert mutated.status == "unresolved", cmd
