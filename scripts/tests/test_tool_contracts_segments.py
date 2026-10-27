"""Per-segment resolver (`tool_contracts.resolve_segments`): every operator, git-write form, `cd`
re-basing rule and redirect, replayed against the denial fixture's decidable cases.

Run with `-s` to print what the resolver returns for a representative set of commands.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

assert importlib.util.find_spec("agentctl.tool_contracts") is not None, "agentctl.tool_contracts must exist"
assert importlib.util.find_spec("agentctl.segment_resolver") is not None, "agentctl.segment_resolver must exist"

from agentctl import resources, tool_contracts

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "resource_grant_replay" / "denials.jsonl"


def _cases():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _make_repo(path: Path, branch: str = "feature") -> Path:
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    return path


@pytest.fixture
def world(tmp_path):
    venue = _make_repo(tmp_path / "venue")
    outside = tmp_path / "outside"
    _make_repo(outside / "canonical-checkout", "main")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (venue / "sub").mkdir()
    return {"venue": venue, "outside": outside, "scratch": scratch, "tmp": tmp_path}


def _subst(text, world):
    return (
        text.replace("{VENUE}", str(world["venue"]))
        .replace("{OUTSIDE}", str(world["outside"]))
        .replace("{SCRATCH}", str(world["scratch"]))
    )


def _res(effect):
    if effect["kind"] == "file":
        return resources.FileResource(effect["path"], effect["op"])
    return resources.VcsRefResource(effect["remote"], effect["ref"], effect["op"])


def _file(path, mode="write"):
    return resources.FileResource(str(path), mode)


def _seg(text, venue, **kw):
    return tool_contracts.resolve_segments(text, str(venue), **kw)


def _statuses(result):
    return [s.status for s in result.segments]


_BASH_CASES = [c for c in _cases() if c["tool"] == "Bash"]
_DECIDABLE = [c for c in _BASH_CASES if not c["opaque"]]
_OPAQUE = [c for c in _BASH_CASES if c["opaque"]]


@pytest.mark.parametrize("case", _DECIDABLE, ids=lambda c: c["id"])
def test_fixture_case_resolves_to_its_expected_effects(case, world):
    _make_repo_for_case(case, world)
    result = _seg(_subst(case["command"], world), world["venue"])
    assert all(s.status == "resolved" for s in result.segments), [(s.text, s.status, s.reason) for s in result.segments]
    expected = {_res({**e, "path": _subst(e["path"], world)} if "path" in e else e) for e in case["expected_effects"]}
    assert set(result.resources) == expected


def _make_repo_for_case(case, world):
    head = world["venue"] / ".git" / "HEAD"
    head.write_text(f"ref: refs/heads/{case['branch']}\n")


@pytest.mark.parametrize("case", _OPAQUE, ids=lambda c: c["id"])
def test_fixture_opaque_case_is_opaque_with_its_kind(case, world):
    _make_repo_for_case(case, world)
    result = _seg(_subst(case["command"], world), world["venue"])
    assert [s.opaque_kind for s in result.segments if s.status == "opaque"] == [case["opaque_kind"]]
    assert not [s for s in result.segments if s.status == "unresolved"]


def test_every_fixture_bash_case_is_covered_by_a_test():
    assert len(_DECIDABLE) + len(_OPAQUE) == len(_BASH_CASES) > 0


class TestOperators:
    @pytest.mark.parametrize("op", ["&&", ";", "|", "||", "&", "\n"])
    def test_each_operator_splits_into_two_segments(self, op, world):
        result = _seg(f"echo a {op} echo b", world["venue"])
        assert [s.text for s in result.segments] == ["echo a", "echo b"]
        assert _statuses(result) == ["resolved", "resolved"]

    def test_operators_inside_quotes_do_not_split(self, world):
        result = _seg('echo "a && b; c | d" \'x ; y\'', world["venue"])
        assert len(result.segments) == 1

    def test_heredoc_body_is_never_scanned_for_operators(self, world):
        result = _seg("cat <<'EOF'\na && b; rm -rf /\nEOF\necho done", world["venue"])
        assert [s.status for s in result.segments] == ["resolved", "resolved"]
        assert len(result.segments) == 2

    def test_heredoc_before_a_pipe_still_attaches_its_body(self, world):
        result = _seg("cat <<EOF | tail -1\nx; y\nEOF\necho done", world["venue"])
        assert _statuses(result) == ["resolved", "resolved", "resolved"]

    @pytest.mark.parametrize("text", ["echo $(date)", "echo `date`", "(cd /; ls)", "for i in 1; do ls; done", "echo <(ls)"])
    def test_nested_execution_is_unresolved(self, text, world):
        assert "unresolved" in _statuses(_seg(text, world["venue"]))

    def test_unterminated_quote_is_one_unresolved_segment(self, world):
        result = _seg('echo "oops && ls', world["venue"])
        assert _statuses(result) == ["unresolved"]
        assert result.segments[0].reason_class == "residual-syntax"

    def test_one_undecidable_segment_does_not_hide_the_others(self, world):
        result = _seg("touch a.txt && frobnicate x; touch /abs/b", world["venue"])
        assert _statuses(result) == ["resolved", "unresolved", "resolved"]
        assert result.segments[0].resources == [_file(world["venue"] / "a.txt")]
        assert result.segments[1].reason_class == "unknown-program"

    def test_unknown_program_and_assignment_and_wrapper_are_unresolved(self, world):
        for text, cls in [("frobnicate x", "unknown-program"), ("FOO=1 ls", "contract-unresolved"), ("env ls", "contract-unresolved")]:
            seg = _seg(text, world["venue"]).segments[0]
            assert (seg.status, seg.reason_class) == ("unresolved", cls), text


class TestCd:
    def test_cd_rebases_an_and_chained_successor(self, world):
        result = _seg("cd sub && touch f", world["venue"])
        assert result.segments[1].resources == [_file(world["venue"] / "sub" / "f")]
        assert result.segments[1].venue == str(world["venue"] / "sub")

    def test_cd_chain_accumulates(self, world):
        result = _seg("cd sub && cd .. && touch f", world["venue"])
        assert result.segments[2].resources == [_file(world["venue"] / "f")]

    def test_absolute_cd_then_relative_write(self, world):
        result = _seg(f"cd {world['outside']} && touch r", world["venue"])
        assert result.segments[1].resources == [_file(world["outside"] / "r")]

    def test_relative_segment_after_semicolon_following_a_cd_is_unresolved(self, world):
        result = _seg("cd sub; touch f", world["venue"])
        assert _statuses(result) == ["resolved", "unresolved"]
        assert result.segments[1].reason_class == "contract-unresolved"

    def test_absolute_segment_after_semicolon_following_a_cd_still_resolves(self, world):
        result = _seg(f"cd sub; touch {world['outside']}/abs", world["venue"])
        assert result.segments[1].resources == [_file(world["outside"] / "abs")]

    def test_semicolon_without_any_cd_keeps_the_venue(self, world):
        result = _seg("touch a; touch b", world["venue"])
        assert result.segments[1].resources == [_file(world["venue"] / "b")]

    def test_or_after_a_cd_is_unresolved_for_relative_segments(self, world):
        result = _seg("cd sub && touch a || touch b", world["venue"])
        assert result.segments[1].resources == [_file(world["venue"] / "sub" / "a")]
        assert result.segments[2].status == "unresolved"

    def test_cd_inside_a_pipeline_moves_nothing_for_sure(self, world):
        result = _seg("cd sub | cat && touch f", world["venue"])
        assert result.segments[-1].status == "unresolved"

    @pytest.mark.parametrize("cd", ["cd $HOME", "cd -", "cd", "cd ~", "cd sub/*"])
    def test_unreadable_cd_target_makes_later_relative_segments_unresolved(self, cd, world):
        result = _seg(f"{cd} && touch f", world["venue"])
        assert result.segments[-1].status == "unresolved"

    def test_a_residual_syntax_segment_poisons_the_cwd(self, world):
        result = _seg("echo $(date) && touch f", world["venue"])
        assert result.segments[-1].status == "unresolved"

    def test_pushd_poisons_the_cwd(self, world):
        result = _seg("pushd sub && touch f", world["venue"])
        assert result.segments[-1].status == "unresolved"

    def test_git_without_dash_c_follows_the_rebased_cwd(self, world):
        other = _make_repo(world["tmp"] / "other", "topic")
        result = _seg(f"cd {other} && git commit -q -m x", world["venue"])
        assert set(result.segments[1].resources) == {_file(other), resources.VcsRefResource(".", "topic", "commit")}


class TestRedirects:
    def test_redirect_target_is_a_write(self, world):
        result = _seg("echo hi > out.txt", world["venue"])
        assert result.resources == [_file(world["venue"] / "out.txt")]

    @pytest.mark.parametrize("op", [">", ">>", ">|", "&>", "&>>", "2>", "2>>", "1>"])
    def test_every_write_form(self, op, world):
        result = _seg(f"echo hi {op} out.txt", world["venue"])
        assert result.resources == [_file(world["venue"] / "out.txt")]

    def test_glued_target(self, world):
        assert _seg("echo hi >out.txt", world["venue"]).resources == [_file(world["venue"] / "out.txt")]

    @pytest.mark.parametrize("text", ["echo hi > /dev/null", "echo hi 2>&1", "echo hi 2>/dev/null", "echo hi >&2", "cat < in.txt", "cat <<< word", "echo hi 1>&2"])
    def test_devices_dups_and_inputs_have_no_effect(self, text, world):
        result = _seg(text, world["venue"])
        assert _statuses(result) == ["resolved"] and result.resources == []

    def test_non_literal_target_is_unresolved(self, world):
        seg = _seg("echo hi > $OUT", world["venue"]).segments[0]
        assert seg.status == "unresolved" and seg.reason_class == "residual-syntax"

    def test_redirect_on_an_opaque_segment_is_still_listed(self, world):
        seg = _seg("python3 -c 'print(1)' > out.txt", world["venue"]).segments[0]
        assert seg.status == "opaque" and seg.opaque_kind == "python-c"
        assert seg.resources == [_file(world["venue"] / "out.txt")]

    def test_tee_writes_every_operand(self, world):
        result = _seg("echo hi | tee a.txt b.txt", world["venue"])
        assert set(result.resources) == {_file(world["venue"] / "a.txt"), _file(world["venue"] / "b.txt")}

    def test_tee_append_flag(self, world):
        assert _seg("echo hi | tee -a a.txt", world["venue"]).resources == [_file(world["venue"] / "a.txt")]

    def test_relative_redirect_after_an_uncertain_cwd_is_unresolved(self, world):
        assert _seg("cd sub; echo hi > f", world["venue"]).segments[1].status == "unresolved"

    def test_absolute_redirect_resolves_even_when_cwd_is_uncertain(self, world):
        result = _seg(f"cd sub; echo hi > {world['outside']}/f", world["venue"])
        assert result.segments[1].resources == [_file(world["outside"] / "f")]


class TestGitWrites:
    @pytest.mark.parametrize(
        "sub",
        ["add -A", "add a b", "stash", "stash push -m wip", "restore a", "rm a", "mv a b", "reset --soft HEAD~1", "reset --mixed", "reset HEAD a", "checkout -- a", "switch -c topic"],
    )
    def test_write_subcommand_writes_the_worktree_root(self, sub, world):
        result = _seg(f"git {sub}", world["venue"])
        assert result.resources == [_file(world["venue"])], sub

    @pytest.mark.parametrize("sub", ["commit -q -m msg", "merge topic", "commit -am msg"])
    def test_commit_and_merge_add_the_current_branch(self, sub, world):
        result = _seg(f"git {sub}", world["venue"])
        assert set(result.resources) == {_file(world["venue"]), resources.VcsRefResource(".", "feature", "commit")}

    def test_dash_c_resolves_against_that_directory(self, world):
        result = _seg(f"git -C {world['outside']}/canonical-checkout commit -q -m x", world["venue"])
        assert set(result.resources) == {
            _file(world["outside"] / "canonical-checkout"),
            resources.VcsRefResource(".", "main", "commit"),
        }

    def test_dash_c_to_the_venue_is_equivalent_to_no_dash_c(self, world):
        assert _seg(f"git -C {world['venue']} add -A", world["venue"]).resources == _seg("git add -A", world["venue"]).resources

    def test_dash_c_relative_follows_cwd(self, world):
        assert _seg("git -C sub add -A", world["venue"]).resources == [_file(world["venue"])]

    def test_subdirectory_resolves_to_the_worktree_root(self, world):
        assert _seg(f"git -C {world['venue']}/sub add x", world["venue"]).resources == [_file(world["venue"])]

    def test_linked_worktree_reads_branch_from_its_gitdir(self, world):
        gitdir = world["tmp"] / "main-repo" / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/linked\n")
        wt = world["tmp"] / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {gitdir}\n")
        result = _seg(f"git -C {wt} commit -m x", world["venue"])
        assert set(result.resources) == {_file(wt), resources.VcsRefResource(".", "linked", "commit")}

    def test_detached_head_commit_is_unresolved(self, world):
        (world["venue"] / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
        assert _seg("git commit -m x", world["venue"]).segments[0].status == "unresolved"

    def test_outside_any_worktree_is_unresolved(self, world, tmp_path_factory):
        bare = tmp_path_factory.mktemp("bare")
        assert _seg(f"git -C {bare} add -A", world["venue"]).segments[0].status == "unresolved"

    def test_commit_after_a_branch_switch_in_the_same_line_is_unresolved(self, world):
        result = _seg("git checkout topic && git commit -m x", world["venue"])
        assert _statuses(result) == ["resolved", "unresolved"]

    def test_file_checkout_does_not_stale_the_branch(self, world):
        result = _seg("git checkout -- a && git commit -m x", world["venue"])
        assert _statuses(result) == ["resolved", "resolved"]

    @pytest.mark.parametrize("sub", ["reset --hard", "reset --hard HEAD~1", "reset --merge", "add $F", "add *.py", "add a/*"])
    def test_destructive_or_non_literal_forms_are_unresolved(self, sub, world):
        assert _seg(f"git {sub}", world["venue"]).segments[0].status == "unresolved", sub

    def test_non_literal_message_does_not_matter(self, world):
        result = _seg('git commit -q -m "fix $THING"', world["venue"])
        assert result.segments[0].status == "resolved"

    def test_command_substitution_in_the_message_is_unresolved(self, world):
        assert _seg('git commit -m "$(date)"', world["venue"]).segments[0].status == "unresolved"

    @pytest.mark.parametrize("flag", ["--git-dir=/x", "--work-tree=/x", "-c", "--exec-path=/x"])
    def test_other_global_flags_are_unresolved(self, flag, world):
        assert _seg(f"git {flag} add -A", world["venue"]).segments[0].status == "unresolved"

    def test_push_literal_resolves_to_a_push_resource(self, world):
        assert _seg("git push origin topic", world["venue"]).resources == [resources.VcsRefResource("origin", "topic", "push")]
        assert _seg("git push -q origin HEAD:main", world["venue"]).resources == [resources.VcsRefResource("origin", "main", "push")]

    @pytest.mark.parametrize("push", ["git push --force origin topic", "git push origin +topic", "git push origin :topic", "git push origin --delete topic", "git push"])
    def test_push_force_delete_and_bare_are_unresolved(self, push, world):
        assert _seg(push, world["venue"]).segments[0].status == "unresolved"

    def test_read_only_git_has_no_effect(self, world):
        for text in ["git status --porcelain", "git log --oneline -5", "git diff a.py", "git rev-parse --short HEAD"]:
            result = _seg(text, world["venue"])
            assert _statuses(result) == ["resolved"] and result.resources == [], text


class TestOtherPrograms:
    def test_mkdir_and_touch_write_each_operand(self, world):
        assert _seg("mkdir -p a/b c", world["venue"]).resources == [_file(world["venue"] / "a/b"), _file(world["venue"] / "c")]
        assert _seg("touch x y", world["venue"]).resources == [_file(world["venue"] / "x"), _file(world["venue"] / "y")]

    def test_mv_writes_all_operands_and_cp_only_the_destination(self, world):
        assert set(_seg("mv a b", world["venue"]).resources) == {_file(world["venue"] / "a"), _file(world["venue"] / "b")}
        assert _seg("cp a b", world["venue"]).resources == [_file(world["venue"] / "b")]

    @pytest.mark.parametrize("text", ["mv -t dir a", "cp --target-directory=d a", "mv a", "touch -r ref f", "mkdir -m 777 d", "mv a $B"])
    def test_unreviewed_flags_and_shapes_are_unresolved(self, text, world):
        assert _seg(text, world["venue"]).segments[0].status == "unresolved", text

    def test_awk_print_program_has_no_effect(self, world):
        result = _seg("awk -F'\\t' '{print $1,$3}' data.tsv", world["venue"])
        assert _statuses(result) == ["resolved"] and result.resources == []

    @pytest.mark.parametrize("prog", ["{print > \"x\"}", "{print | \"sh\"}", "BEGIN{system(\"ls\")}", "{\"ls\" | getline}"])
    def test_awk_program_that_redirects_or_executes_is_unresolved(self, prog, world):
        assert _seg(f"awk '{prog}' f", world["venue"]).segments[0].status == "unresolved", prog

    @pytest.mark.parametrize(
        "text, kind",
        [
            ("python3 -c 'print(1)'", "python-c"),
            ("python3 -W ignore -c 'print(1)'", "python-c"),
            ("python3 - <<'EOF'\nprint(1)\nEOF", "stdin-body"),
            ("python3 <<'EOF'\nprint(1)\nEOF", "stdin-body"),
            ("bash <<'EOF'\nls\nEOF", "stdin-body"),
            ("sh -", "stdin-body"),
            ("python3 some_script.py", "script-file"),
            ("bash run.sh", "script-file"),
        ],
    )
    def test_interpreters_with_unreadable_code_are_opaque(self, text, kind, world):
        seg = _seg(text, world["venue"]).segments[0]
        assert (seg.status, seg.opaque_kind) == ("opaque", kind)

    @pytest.mark.parametrize("text", ["bash -c 'ls'", "sh -ec 'ls'", "node -e 'x'", "perl -e 'x'", "python3 -m http.server"])
    def test_inline_code_other_than_python_c_is_unresolved(self, text, world):
        assert _seg(text, world["venue"]).segments[0].status == "unresolved", text

    def test_python_m_pytest_goes_through_the_registry_resolver(self, world):
        seg = _seg("python3 -m pytest -q scripts/tests/test_x.py", world["venue"]).segments[0]
        assert seg.status == tool_contracts.resolve_command("python3 -m pytest -q scripts/tests/test_x.py", str(world["venue"])).status

    def test_table_unresolved_program_stays_unresolved(self, world):
        seg = _seg("rm -rf x", world["venue"]).segments[0]
        assert (seg.status, seg.reason_class) == ("unresolved", "contract-unresolved")

    def test_none_effect_program_with_unreviewed_flag_is_unresolved(self, world):
        table = tool_contracts.load_contract_table()
        rg = _seg("rg --pre sh pattern", world["venue"], contract_table=table).segments[0]
        assert rg.status == "unresolved"

    def test_path_qualified_program_is_unresolved(self, world):
        assert _seg("/bin/ls", world["venue"]).segments[0].status == "unresolved"


class TestResolveCommandIsUnchanged:
    """Segment mode must not loosen the all-or-nothing resolver every other caller relies on."""

    @pytest.mark.parametrize(
        "text",
        [
            "git add -A",
            "git commit -q -m x",
            "git -C /somewhere/else status",
            "echo hi > out.txt",
            "cd sub && touch f",
            "touch a",
            "mkdir -p d",
            "python3 -c 'print(1)'",
            "cat a | tee b",
        ],
    )
    def test_writes_and_redirects_stay_unresolved_there(self, text, world):
        assert tool_contracts.resolve_command(text, str(world["venue"])).status == "unresolved", text

    @pytest.mark.parametrize("text", ["git status --porcelain", "git log --oneline", "cat a | head -2", "git push origin topic"])
    def test_read_only_forms_stay_resolved_there(self, text, world):
        assert tool_contracts.resolve_command(text, str(world["venue"])).status == "resolved", text


def test_behaviour_printout(world):
    venue, outside = world["venue"], world["outside"]
    commands = [
        "git add -A && git commit -q -m x; git push -q origin feature 2>&1 | tail -2; git rev-parse --short HEAD",
        f"git -C {outside}/canonical-checkout commit -q -m x",
        "git checkout topic && git commit -m y",
        "git stash; git restore a; git reset --soft HEAD~1",
        "cd sub && touch f",
        "cd sub; touch f",
        "cd sub | cat && touch f",
        "echo hi > out.txt && echo no > /dev/null 2>&1",
        "echo hi | tee -a a.txt",
        "python3 -c 'print(1)' > o.txt",
        "python3 - <<'EOF'\nopen('x','w')\nEOF",
        "python3 tool.py",
        "echo $(date)",
        "rm -rf x",
    ]
    for cmd in commands:
        result = _seg(cmd, venue)
        print(f"\n$ {cmd!r}")
        for seg in result.segments:
            shown = [f"{type(r).__name__}({os.path.relpath(getattr(r, 'path', ''), str(venue.parent)) if hasattr(r, 'path') else (r.remote, r.ref, r.op)})" for r in seg.resources]
            print(f"  [{seg.status}] {seg.text!r} resources={shown} opaque_kind={seg.opaque_kind} reason_class={seg.reason_class}")
