"""Unit coverage for lib.git_cwd.effective_git_cwd — the shared resolver for the
tree a VCS commit command actually targets (extracted from
hook-guard-canon-readonly.py, #44). Both redirect branches (`git -C <dir>` and a
leading `cd <dir>`) plus the fallback-to-payload_cwd path are covered so the
extraction stays behavior-identical to the original."""
from __future__ import annotations

import os

from lib import git_cwd, shell_tokens


def test_cd_redirect_absolute():
    assert git_cwd.effective_git_cwd("cd /repo/b && git commit -m x", "/repo/a") == "/repo/b"


def test_cd_redirect_relative_resolved_against_payload_cwd():
    assert git_cwd.effective_git_cwd("cd sub && git commit -m x", "/repo/a") == os.path.join("/repo/a", "sub")


def test_dash_c_absolute():
    assert git_cwd.effective_git_cwd("git -C /repo/b commit -m x", "/repo/a") == "/repo/b"


def test_dash_c_relative_resolved_against_payload_cwd():
    assert git_cwd.effective_git_cwd("git -C sub commit -m x", "/repo/a") == os.path.join("/repo/a", "sub")


def test_bare_commit_no_redirect_returns_payload_cwd():
    assert git_cwd.effective_git_cwd("git commit -m x", "/repo/a") == "/repo/a"


def test_non_git_vcs_commit_with_cd_redirect():
    # the leading-cd branch is command-agnostic, so it resolves for any VCS
    assert git_cwd.effective_git_cwd("cd /repo/b && othervcs commit -m x", "/repo/a") == "/repo/b"


def test_dash_c_before_non_commit_verb_does_not_redirect():
    # the -C scan requires tokens[i+3] == "commit"; `status` must not redirect
    assert git_cwd.effective_git_cwd("git -C /repo/b status", "/repo/a") == "/repo/a"


def test_unparseable_command_falls_back_to_payload_cwd():
    # an unbalanced quote makes shlex.split raise -> fallback, never a wilder guess
    assert git_cwd.effective_git_cwd('git commit -m "unterminated', "/repo/a") == "/repo/a"


def test_leading_cd_missing_dir_semicolon_does_not_relocate(tmp_path):
    # the leading `cd` fails at runtime; `;` runs the next segment unconditionally
    # in payload_cwd anyway, so the redirect must NOT be honored
    missing = tmp_path / "does-not-exist"
    payload_cwd = str(tmp_path)
    assert git_cwd.effective_git_cwd(f"cd {missing} ; echo b > s2", payload_cwd) == payload_cwd


def test_leading_cd_to_existing_dir_still_relocates(tmp_path):
    # the `cd` succeeds, so the redirect is honored exactly as for `&&` — the
    # safe-shape check only ever narrows the `;` case, never the `&&` one
    target = tmp_path / "exists"
    target.mkdir()
    assert git_cwd.effective_git_cwd(f"cd {target} ; echo b > s2", str(tmp_path)) == str(target)


def test_git_C_wins_over_failed_leading_cd(tmp_path):
    # precedence: the `-C <dir> commit` scan must win even when a leading `cd`
    # to a missing dir would otherwise be recognized as the safe no-relocate shape
    missing = tmp_path / "does-not-exist"
    worktree = tmp_path / "worktree"
    assert git_cwd.effective_git_cwd(
        f"cd {missing} ; git -C {worktree} commit -m x", str(tmp_path)
    ) == str(worktree)


def _segments(command):
    return [seg for _sep, seg in
            shell_tokens.split_segments(shell_tokens.tokenize(command)) if seg]


def test_segment_git_cwd_is_per_segment():
    # `-C` moves ONE git invocation and is not transitive: the `status` segment
    # runs in /repo/b, the `commit` segment right after it does not
    first, second = _segments("git -C /repo/b status ; git commit -m x")
    assert git_cwd.segment_git_cwd(first, "/repo/a") == "/repo/b"
    assert git_cwd.segment_git_cwd(second, "/repo/a") == "/repo/a"
    # and the commit-scoped resolver agrees: it must not carry the `status`
    # segment's `-C` across the separator
    assert git_cwd.effective_git_cwd("git -C /repo/b status ; git commit -m x", "/repo/a") == "/repo/a"
    # cumulative within ONE segment, each resolved against the previous
    # (measured against git 2.43.0: `-C cum/x -C y` reports cum/x/y/.git)
    (both,) = _segments("git -C /repo/b -C sub commit -m x")
    assert git_cwd.segment_git_cwd(both, "/repo/a") == os.path.join("/repo/b", "sub")


def test_first_committing_segment_wins_over_a_later_dash_C():
    # The ONE guard-verdict change the per-segment rule produces, pinned here so
    # it stays deliberate. The pre-stage adjacency scan found `-C /repo/b`
    # anywhere in the command and applied it command-wide, so this resolved to
    # /repo/b and the guard allowed it. The first COMMITTING segment carries no
    # `-C`, so it really does commit in the session's own cwd -- the old reading
    # was a lost deny, not a permission this stage removes.
    command = "git commit -m x && git -C /repo/b commit -m y"
    assert git_cwd.effective_git_cwd(command, "/repo/a") == "/repo/a"
    first, second = _segments(command)
    assert git_cwd.segment_git_cwd(first, "/repo/a") == "/repo/a"
    assert git_cwd.segment_git_cwd(second, "/repo/a") == "/repo/b"


def test_runs_commit_doubts_relocating_globals():
    for command in (
        "git --git-dir=/repo/a/.git commit -m x",
        "git --work-tree=/repo/b commit -m x",
        "git --namespace=ns commit -m x",
        "git --git-dir /repo/a/.git --work-tree /repo/b commit -m x",
    ):
        assert git_cwd.runs_commit(command) is None, command
        # containment is bought by shrinking the DETECTOR, not by growing the
        # resolver: `segment_git_cwd` reads `-C` only, so these move nothing
        (segment,) = _segments(command)
        assert git_cwd.segment_git_cwd(segment, "/repo/a") == "/repo/a", command
    # the doubt is scoped to the commit; a non-commit subcommand carrying the
    # same global stays a plain False
    assert git_cwd.runs_commit("git --git-dir=/repo/a/.git status") is False


def test_redirect_target_uses_shell_cwd_not_git_C():
    # measured 2026-08-03: `cd here && git -C ../other status > notes.md`
    # creates here/notes.md, never other/notes.md -- the shell opens the
    # redirect in ITS cwd before git runs
    command = "cd /repo/here && git -C /repo/other status > notes.md"
    # direction 1: the redirect is opened in the SHELL's cwd
    assert git_cwd.command_default_cwd(command, "/repo/a") == "/repo/here"
    # direction 2: git's own repo operation really does happen in the `-C` dir
    segment = _segments(command)[-1]
    assert git_cwd.segment_git_cwd(segment, "/repo/here") == "/repo/other"


def test_runs_commit_tri_state():
    assert git_cwd.runs_commit("git commit -m x") is True
    assert git_cwd.runs_commit("sudo -n git commit -m x") is True
    assert git_cwd.runs_commit("git status") is False
    assert git_cwd.runs_commit("cd /tmp && ls") is False
    # the five doubt producers, one command each
    assert git_cwd.runs_commit('git commit -m "unterminated') is None    # 1 tokenizer raises
    assert git_cwd.runs_commit("sudo -h git commit -m x") is None        # 2 prefix stripper
    assert git_cwd.runs_commit("git --frobnicate commit -m x") is None   # 3 scanner doubt
    assert git_cwd.runs_commit("git bisect run ./t.sh") is None          # 4 command-taking
    assert git_cwd.runs_commit("git --git-dir=/r/.git commit -m x") is None  # 5 relocating
    # a print-and-exit global runs no subcommand at all -- that is KNOWN, not
    # doubted, so it stays False
    assert git_cwd.runs_commit("git --version commit") is False
    # any one provable commit segment wins over doubt elsewhere
    assert git_cwd.runs_commit("sudo -h ls ; git commit -m x") is True
