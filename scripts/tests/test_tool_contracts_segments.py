"""Per-segment resolver (`tool_contracts.resolve_segments`): every operator, git-write form, `cd`
re-basing rule and redirect, replayed against the denial fixture (`denials.jsonl`) and against a
table of probe lines that each pin one fail-closed decision.

The resolver must under-resolve rather than over-resolve: an unresolved segment costs one prompt,
an over-resolved one silently auto-allows. So the tests pin both directions -- what resolves, with
exactly which resources, and what stays unresolved.

To see what the resolver returns for every probe line:
    PYTHONPATH=scripts python3 scripts/tests/test_tool_contracts_segments.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agentctl import resources, segment_resolver, tool_contracts

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "resource_grant_replay" / "denials.jsonl"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_replay_fixture  # noqa: E402


@pytest.fixture(autouse=True)
def _no_cdpath(monkeypatch):
    monkeypatch.delenv("CDPATH", raising=False)


def _git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True, capture_output=True,
    )


def _init_repo(path: Path, branch: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True, capture_output=True)
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def build_world(tmp: Path) -> dict:
    tmp = tmp.resolve()
    venue = _init_repo(tmp / "venue", "feature")
    (venue / "sub" / "deep").mkdir(parents=True)
    (venue / "scripts" / "tests" / "fixtures").mkdir(parents=True)
    (tmp / "elsewhere" / "x" / "y").mkdir(parents=True)
    (venue / "lnk").symlink_to(tmp / "elsewhere" / "x" / "y")
    _git(venue, "worktree", "add", "-q", "-b", "linked", str(tmp / "wt"))
    outside = tmp / "outside"
    other = _init_repo(outside / "canonical-checkout", "main")
    _git(venue, "branch", "topic")
    _git(venue, "tag", "v1")
    _git(venue, "tag", "agent/x")
    _git(venue, "branch", "both")
    _git(venue, "tag", "both")
    _git(venue, "remote", "add", "origin", str(other))
    _git(venue, "update-ref", "refs/remotes/origin/remote-only", "HEAD")
    scratch = tmp / "scratch"
    scratch.mkdir()
    return {"venue": venue, "outside": outside, "other": other, "scratch": scratch, "wt": tmp / "wt", "tmp": tmp}


@pytest.fixture(scope="module")
def _shared_world(tmp_path_factory):
    return build_world(tmp_path_factory.mktemp("segments"))


@pytest.fixture
def world(_shared_world):
    """The shared repositories; a test that moves HEAD is undone afterwards."""
    yield _shared_world
    _git(_shared_world["venue"], "checkout", "-q", "-B", "feature")


def _subst(text, world):
    return (
        text.replace("{VENUE}", str(world["venue"]))
        .replace("{OUTSIDE}", str(world["outside"]))
        .replace("{OTHER}", str(world["other"]))
        .replace("{WT}", str(world["wt"]))
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


def _cases():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


_BASH_CASES = [c for c in _cases() if c["tool"] == "Bash"]
_DECIDABLE = [c for c in _BASH_CASES if not c["opaque"]]
_OPAQUE = [c for c in _BASH_CASES if c["opaque"]]


def _checkout_branch(world, case):
    _git(world["venue"], "branch", "-M", case["branch"])


@pytest.mark.parametrize("case", _DECIDABLE, ids=lambda c: c["id"])
def test_fixture_case_resolves_to_its_expected_effects(case, world):
    _checkout_branch(world, case)
    result = _seg(_subst(case["command"], world), world["venue"])
    if case.get("resolver") == "fall-through":
        assert _statuses(result) == ["unresolved"]
        assert result.resources == []
        return
    assert set(_statuses(result)) == {"resolved"}, [(s.text, s.status, s.reason) for s in result.segments]
    expected = {_res({**e, "path": _subst(e["path"], world)} if "path" in e else e) for e in case["expected_effects"]}
    assert set(result.resources) == expected


@pytest.mark.parametrize("case", _OPAQUE, ids=lambda c: c["id"])
def test_fixture_opaque_case_is_opaque_with_its_kind(case, world):
    _checkout_branch(world, case)
    result = _seg(_subst(case["command"], world), world["venue"])
    assert [s.opaque_kind for s in result.segments if s.status == "opaque"] == [case["opaque_kind"]]
    assert set(_statuses(result)) <= {"resolved", "opaque"}


def test_fixture_passes_its_own_checker():
    readme = FIXTURE.with_name("README.md")
    check_replay_fixture.check(FIXTURE.read_text().splitlines(keepends=True), readme.read_text(), str(readme))


def test_the_fall_through_cases_are_the_foreign_commit_and_the_bare_push_destinations():
    assert [c["id"] for c in _DECIDABLE if c.get("resolver") == "fall-through"] == [
        "prefix-mismatch-02", "prefix-mismatch-03", "prefix-mismatch-04", "uncovered-write-07",
    ]


# --- probe lines: one pinned decision each -----------------------------------------------------

def W(path):
    return ("file", path)


def C(branch="feature"):
    return ("vcs", ".", branch, "commit")


def P(remote, ref):
    return ("vcs", remote, ref, "push")


def R(*res):
    return ("resolved", set(res))


U = ("unresolved", None)


def O(kind):
    return ("opaque", kind)


V = "{VENUE}"

#: (probe id, command, expected [(status, resources | opaque kind)] per segment)
PROBES = [
    # B1 stash is a ref-moving write nobody named
    ("B1-stash", "git stash", [U]),
    ("B1-stash-push", "git stash push -m wip", [U]),
    ("B1-stash-pop-commit", "git stash pop && git commit -q -m x", [U, U]),
    ("B1-stash-list", "git stash list", [U]),
    # B2 a checkout/switch that creates, resets or forces a branch ref
    ("B2-checkout-B", "git checkout -B topic", [U]),
    ("B2-checkout-b", "git checkout -b topic", [U]),
    ("B2-switch-C", "git switch -C topic", [U]),
    ("B2-switch-c", "git switch -c topic", [U]),
    ("B2-switch-force-create", "git switch --force-create topic", [U]),
    ("B2-checkout-orphan", "git checkout --orphan topic", [U]),
    ("B2-checkout-force", "git checkout -f topic", [U]),
    ("B2-checkout-detach", "git checkout --detach", [U]),
    # B3 a commit-ish reset moves the current branch ref
    ("B3-reset-soft", "git reset --soft HEAD~1", [U]),
    ("B3-reset-commitish", "git reset HEAD~3", [U]),
    ("B3-reset-hard", "git reset --hard", [U]),
    ("B3-reset-merge", "git reset --merge", [U]),
    ("B3-reset-path", "git reset HEAD a", [R(W(V))]),
    ("B3-reset-bare", "git reset", [R(W(V))]),
    # B4 a cd reached through || is conditional
    ("B4-or-cd-and", "ls || cd /etc && touch f", [R(), R(), U]),
    ("B4-cd-or-cd-and", "cd /x || cd /y && touch f", [R(), R(), U]),
    ("B4-cd-or", "cd sub || touch f", [R(), U]),
    # B5 a branch switch concurrent with, or before, a commit
    ("B5-commit-async-checkout", "git commit -q -m x & git checkout topic", [U, R(W(V))]),
    ("B5-commit-pipe-checkout", "git commit -q -m x | git checkout topic", [U, R(W(V))]),
    ("B5-checkout-then-commit", "git checkout topic && git commit -q -m x", [R(W(V)), U]),
    ("B5-commit-then-checkout", "git commit -q -m x && git checkout topic", [R(W(V), C()), R(W(V))]),
    ("B5-file-checkout-keeps-branch", "git checkout -- a && git commit -q -m x", [R(W(V)), R(W(V), C())]),
    ("B5-push-head-async-checkout", "git push origin HEAD & git checkout topic", [U, R(W(V))]),
    # B6 a command substitution in an unquoted heredoc body runs
    ("B6-heredoc-subst", "cat <<EOF > out\n$(id)\nEOF", [U]),
    ("B6-heredoc-backtick", "cat <<EOF\n`touch /tmp/pwn`\nEOF", [U]),
    ("B6-heredoc-arith", "cat <<EOF\n$((1+1))\nEOF", [U]),
    ("B6-quoted-heredoc-inert", "cat <<'EOF' > out\n$(id)\nEOF", [R(W(V + "/out"))]),
    ("B6-unquoted-heredoc-plain-var", "cat <<EOF > out\nhello $HOME\nEOF", [R(W(V + "/out"))]),
    # B7 $'..' escapes decide where the quote ends
    ("B7-ansi-c-quote", "echo $'\\'' ; touch /etc/zz #'", [U]),
    # B8 `..` after a symlink
    ("B8-symlink-dotdot", "cd lnk/.. && touch f", [R(), U]),
    ("B8-plain-dotdot", "cd sub/.. && touch f", [R(), R(W(V + "/f"))]),
    # B9 / S5 another repository
    ("B9-dash-C-foreign-push", "git -C {OTHER} push origin feature", [U]),
    ("B9-cd-foreign-push", "cd {OTHER} && git push origin feature", [R(), U]),
    ("S5-foreign-readonly", "git -C {OTHER} status", [U]),
    ("S5-own-subdir-readonly", "git -C {VENUE}/sub status", [R()]),
    ("S5-linked-worktree-commit", "git -C {WT} commit -q -m x", [R(W("{WT}"), C("linked"))]),
    # S1 anything unreadable before a commit makes the branch unknowable
    ("S1-rebase-commit", "git rebase main && git commit -q -m x", [U, U]),
    ("S1-config-flag-checkout-commit", "git -c x=y checkout topic && git commit -q -m x", [U, U]),
    ("S1-opaque-before-commit", "python3 s.py && git commit -q -m x", [O("script-file"), U]),
    ("S1-unknown-before-commit", "frobnicate; git commit -q -m x", [U, U]),
    # S2 an opaque segment needs a known cwd
    ("S2-opaque-c-after-unreadable-cd", "cd $HOME && python3 -c 'print(1)'", [R(), U]),
    ("S2-opaque-script-after-unreadable-cd", "cd $HOME && python3 s.py", [R(), U]),
    ("S2-opaque-stdin-after-semicolon-cd", "cd sub; python3 - <<'E'\nprint(1)\nE", [R(), U]),
    ("S2-opaque-in-known-cwd", "cd sub && python3 -c 'print(1)'", [R(), O("python-c")]),
    # S3 per-operator cd behaviour
    ("S3-and", "cd sub && touch f", [R(), R(W(V + "/sub/f"))]),
    ("S3-semicolon", "cd sub; touch f", [R(), U]),
    ("S3-async", "cd sub & touch f", [R(), U]),
    ("S3-newline", "cd sub\ntouch f", [R(), U]),
    ("S3-pipe", "cd sub | cat && touch f", [R(), R(), U]),
    ("S3-brace-group", "{ cd sub; } && touch f", [U, U, U]),
    ("S3-subshell", "( cd sub ) && touch f", [U, U]),
    ("S3-bash-c", "bash -c 'cd sub' && touch f", [U, U]),
    # S4 an unreadable operand of a program with a closed flag set
    ("S4-rg-var-operand", "rg $X pat", [U]),
    ("S4-rg-literal", "rg pat file", [R()]),
    ("S4-grep-any-flag-table", "grep $X pat", [R()]),
    # S6 interpreter flag parsing
    ("S6-python-Ic", "python3 -Ic 'code'", [O("python-c")]),
    ("S6-python-W-value-then-c", "python3 -W ignore -c 'code'", [O("python-c")]),
    ("S6-python-Im", "python3 -Im http.server", [U]),
    ("S6-bash-o-value", "bash -o pipefail run.sh", [O("script-file")]),
    ("S6-bash-c", "bash -c ls", [U]),
    # S7 shell keywords and cwd-changing helpers
    ("S7-time-cd", "time cd /x && touch f", [U, U]),
    ("S7-z", "z foo && touch f", [U, U]),
    # S11 push targets are normalised to the branch they name
    ("S11-push-full-ref", "git push origin refs/heads/feature", [R(P("origin", "feature"))]),
    ("S11-push-head", "git push origin HEAD", [R(P("origin", "feature"))]),
    ("S11-push-head-colon", "git push origin HEAD:refs/heads/main", [R(P("origin", "main"))]),
    ("S11-push-src-colon-full-ref", "git push origin feature:refs/heads/main", [R(P("origin", "main"))]),
    ("S11-push-force", "git push --force origin feature", [U]),
    ("S11-push-delete", "git push origin :feature", [U]),
    # round 2 / N1 a bare checkout/switch of a name with only a remote-tracking match creates the branch
    ("R2-N1-checkout-remote-only", "git checkout remote-only", [U]),
    ("R2-N1-switch-remote-only", "git switch remote-only", [U]),
    ("R2-N1-checkout-no-such-branch", "git checkout nosuchbranch", [U]),
    ("R2-N1-switch-no-such-branch", "git switch nosuchbranch", [U]),
    ("R2-N1-checkout-existing-branch", "git checkout topic", [R(W(V))]),
    ("R2-N1-switch-existing-branch", "git switch topic", [R(W(V))]),
    ("R2-N1-checkout-ref-then-paths", "git checkout remote-only -- a", [R(W(V))]),
    ("R2-N1-checkout-traversal-name", "git checkout ../topic", [U]),
    # round 2 / N2 an and-or list that ends in & is backgrounded as a whole
    ("R2-N2-commit-and-ls-bg-checkout", "git commit -q -m x && ls & git checkout topic", [U, R(), R(W(V))]),
    ("R2-N2-push-head-and-ls-bg-checkout", "git push origin HEAD && ls & git checkout topic", [U, R(), R(W(V))]),
    ("R2-N2-commit-bg-then-semicolon-checkout", "git commit -q -m x && ls & ls; git checkout topic", [U, R(), R(), R(W(V))]),
    ("R2-N2-commit-or-bg-checkout", "git commit -q -m x || ls & git checkout topic", [U, R(), R(W(V))]),
    ("R2-N2-sequential-commit-then-checkout", "git commit -q -m x && ls; git checkout topic", [R(W(V), C()), R(), R(W(V))]),
    # round 2 / N3 a symlinked cd target never becomes the tracked cwd: a later `..` is logical in bash
    ("R2-N3-symlink-then-dotdot", "cd lnk && cd .. && touch f", [R(), R(), U]),
    ("R2-N3-symlink-then-two-dotdot", "cd lnk && cd ../.. && touch f", [R(), R(), U]),
    ("R2-N3-symlink-then-relative", "cd lnk && touch f", [R(), U]),
    # round 2 / N4 an awk program that is not literal text
    ("R2-N4-awk-set-positional", "set -- 'BEGIN{system(\"touch /x\")}'; awk \"$1\"", [R(), U]),
    ("R2-N4-awk-bare-var", "awk $P f", [U]),
    ("R2-N4-awk-quoted-var", 'awk "$P" f', [U]),
    ("R2-N4-awk-var-flag-value", "awk -v $V '{print}' f", [U]),
    ("R2-N4-awk-single-quoted-dollar", "awk '{print $1}' f", [R()]),
    # round 2 / N5 HEAD as a push destination creates a remote ref literally called HEAD
    ("R2-N5-push-head-destination", "git push origin feature:HEAD", [U]),
    ("R2-N5-push-full-ref-head-destination", "git push origin feature:refs/heads/HEAD", [U]),
    ("R2-N5-push-refs-heads-HEAD-source", "git push origin refs/heads/HEAD", [U]),
    ("R2-N5-push-head-source-kept", "git push origin HEAD:refs/heads/topic", [R(P("origin", "topic"))]),
    # round 3 / B-R3-1 a bare push destination takes the namespace of the source
    ("R3-B1-tag-source-bare-destination", "git push origin v1:topic", [U]),
    ("R3-B1-colonless-tag", "git push origin agent/x", [U]),
    ("R3-B1-tag-source-full-destination", "git push origin v1:refs/heads/topic", [U]),
    ("R3-B1-branch-and-tag-same-name", "git push origin both:topic", [U]),
    ("R3-B1-branch-and-tag-colonless", "git push origin both", [U]),
    ("R3-B1-at-sign-source", "git push origin @:refs/heads/topic", [R(P("origin", "topic"))]),
    ("R3-B1-at-sign-colonless", "git push origin @", [R(P("origin", "feature"))]),
    ("R3-B1-heads-prefixed-source", "git push origin heads/topic", [U]),
    ("R3-B1-heads-prefixed-destination", "git push origin topic:heads/x", [U]),
    ("R3-B1-tags-prefixed-destination", "git push origin topic:tags/x", [U]),
    ("R3-B1-remote-tracking-source", "git push origin origin/remote-only:topic", [U]),
    ("R3-B1-unknown-name-source", "git push origin nosuchbranch:topic", [U]),
    ("R3-B1-commitish-source", "git push origin HEAD~1:topic", [U]),
    ("R3-B1-full-tag-ref-source", "git push origin refs/tags/v1:topic", [U]),
    ("R3-B1-tag-destination-full-ref", "git push origin topic:refs/tags/x", [U]),
    ("R3-B1-existing-branch-source", "git push origin topic:refs/heads/other", [R(P("origin", "other"))]),
    ("R3-B1-existing-branch-colonless", "git push origin topic", [R(P("origin", "topic"))]),
    ("R3-B1-full-ref-source-full-destination", "git push origin refs/heads/topic:refs/heads/other", [R(P("origin", "other"))]),
    # round 4 / B-R4-1 a bare destination after a colon is matched against the remote's existing refs first
    ("R4-B1-bare-destination", "git push origin topic:other", [U]),
    ("R4-B1-head-bare-destination", "git push origin HEAD:main", [U]),
    ("R4-B1-at-sign-bare-destination", "git push origin @:topic", [U]),
    ("R4-B1-full-ref-source-bare-destination", "git push origin refs/heads/topic:other", [U]),
    ("R4-B1-bare-destination-in-second-refspec", "git push origin topic topic:other", [U]),
    ("R4-B1-colonless-keeps-resolving", "git push origin topic", [R(P("origin", "topic"))]),
    # round 4 / S-R4-1 more than one heredoc or here-string on fd 0 leaves the body that runs ambiguous
    ("R4-S1-two-heredocs", "python3 <<A <<B\nprint(1)\nA\nprint(2)\nB", [U]),
    ("R4-S1-here-string-then-heredoc", "python3 <<< 'print(1)' <<B\nprint(2)\nB", [U]),
    ("R4-S1-two-here-strings", "python3 - <<< 'print(1)' <<< 'print(2)'", [U]),
    ("R4-S1-heredoc-on-other-fd", "python3 <<A 3<<B\nprint(1)\nA\nprint(2)\nB", [O("stdin-body")]),
    ("R4-S1-one-heredoc-stays-opaque", "python3 <<A\nprint(1)\nA", [O("stdin-body")]),
    ("R3-B1-branch-source-moved-by-checkout", "git checkout topic && git push origin HEAD", [R(W(V)), U]),
    # round 3 / S-R3-1 only the last fd-0 redirection decides whether the segment carries its stdin
    ("R3-S1-heredoc-then-file", "python3 <<'E' < s.py\nprint(1)\nE", [U]),
    ("R3-S1-here-string-then-file", "python3 - <<< 'print(1)' 0< s.py", [U]),
    ("R3-S1-heredoc-then-fd-dup", "python3 <<'E' 0<&3\nprint(1)\nE", [U]),
    ("R3-S1-file-then-heredoc", "python3 < s.py <<'E'\nprint(1)\nE", [O("stdin-body")]),
    ("R3-S1-explicit-fd-zero-heredoc", "python3 0<<'E'\nprint(1)\nE", [O("stdin-body")]),
    ("R3-S1-heredoc-then-output-redirect", "python3 <<'E' > {SCRATCH}/o\nprint(1)\nE", [O("stdin-body")]),
    ("R3-S1-heredoc-then-fd-zero-output", "python3 <<'E' 0>{SCRATCH}/o\nprint(1)\nE", [U]),
    # round 2 / N6 mkdir -p creates the missing ancestors
    ("R2-N6-mkdir-p-topmost-new", "mkdir -p {SCRATCH}/new/a/b", [R(W("{SCRATCH}/new"))]),
    ("R2-N6-mkdir-p-existing-parent", "mkdir -p sub/new", [R(W(V + "/sub/new"))]),
    ("R2-N6-mkdir-p-existing-leaf", "mkdir -p sub/deep", [R(W(V + "/sub/deep"))]),
    ("R2-N6-mkdir-p-dotdot", "mkdir -p a/../b/c", [U]),
    ("R2-N6-mkdir-without-p", "mkdir a", [R(W(V + "/a"))]),
    # round 2 / S-a code or script operand that is not literal text is not opaque
    ("R2-Sa-python-c-var", 'python3 -c "$CODE"', [U]),
    ("R2-Sa-python-c-positional", "set -- 'x'; python3 -c \"$1\"", [R(), U]),
    ("R2-Sa-python-script-var", "python3 $S", [U]),
    ("R2-Sa-bash-script-var", "bash $S", [U]),
    ("R2-Sa-python-c-literal", 'python3 -c "print(1)"', [O("python-c")]),
    # round 2 / S-b combined and long-form flags
    ("R2-Sb-perl-ne", "perl -ne 'print' f", [U]),
    ("R2-Sb-ruby-ne", "ruby -ne 'x' f", [U]),
    ("R2-Sb-node-eval-eq", "node --eval=x", [U]),
    ("R2-Sb-node-print-eq", "node --print=x", [U]),
    ("R2-Sb-node-require", "node -r x run.js", [U]),
    ("R2-Sb-node-script", "node run.js", [O("script-file")]),
    ("R2-Sb-perl-script", "perl run.pl", [O("script-file")]),
    ("R2-Sb-bash-s-with-body", "bash -s arg <<'E'\nls\nE", [O("stdin-body")]),
    ("R2-Sb-bash-s-no-body", "bash -s arg", [U]),
    # round 2 / S-c stdin code must be inside the segment to be read
    ("R2-Sc-pipe-into-python", "echo x | python3", [R(), U]),
    ("R2-Sc-pipe-into-python-dash", "echo x | python3 -", [R(), U]),
    ("R2-Sc-file-into-python", "python3 < s.py", [U]),
    ("R2-Sc-here-string", "python3 - <<< 'print(1)'", [O("stdin-body")]),
    ("R2-Sc-here-string-var", 'python3 - <<< "$X"', [U]),
    # nits
    ("N-commit-unquoted-message-var", "git commit -m $MSG", [U]),
    ("N-commit-quoted-message-var", 'git commit -q -m "fix $THING"', [R(W(V), C())]),
    ("N-commit-message-substitution", 'git commit -m "$(date)"', [U]),
    ("N-merge-abort", "git merge --abort", [U]),
    ("N-merge-branch", "git merge topic", [R(W(V), C())]),
    ("N-git-dir-without-work-tree", "git --git-dir={VENUE}/.git add -A", [U]),
    ("N-git-dir-and-work-tree", "git --git-dir={VENUE}/.git --work-tree={VENUE} add -A", [U]),
    ("N-mkdir-p-nested", "mkdir -p a/b", [R(W(V + "/a"))]),
    ("N-commit-plain", "git commit -q -m x", [R(W(V), C())]),
    ("N-commit-combined-flags", "git commit -am x", [R(W(V), C())]),
    ("N-add-all", "git add -A", [R(W(V))]),
    ("N-heredoc-message-commit", "git commit -q -F - <<'EOF'\nmsg $(not run)\nEOF", [R(W(V), C())]),
]


def _norm(resource):
    if isinstance(resource, resources.FileResource):
        return ("file", resource.path)
    return ("vcs", resource.remote, resource.ref, resource.op)


def _expected_resources(expected, world):
    out = set()
    for entry in expected:
        out.add(tuple(_subst(part, world) for part in entry))
    return out


@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p[0])
def test_probe_line(probe, world):
    _, template, expected = probe
    result = _seg(_subst(template, world), world["venue"])
    got = [(s.status, s.opaque_kind) for s in result.segments]
    assert [e[0] for e in expected] == [g[0] for g in got], [(s.text, s.status, s.reason) for s in result.segments]
    for seg, (status, detail) in zip(result.segments, expected):
        if status == "opaque":
            assert seg.opaque_kind == detail
        elif status == "resolved":
            assert {_norm(r) for r in seg.resources} == _expected_resources(detail, world), seg.text


def test_probe_ids_are_unique():
    ids = [p[0] for p in PROBES]
    assert len(ids) == len(set(ids))


def test_heredoc_body_stays_in_its_segment_text(world):
    result = _seg("cat <<'EOF' > out\n$(id) && rm -rf /\nEOF\necho done", world["venue"])
    assert len(result.segments) == 2
    assert "$(id) && rm -rf /" in result.segments[0].text
    assert result.segments[1].text == "echo done"


def test_an_opaque_segment_with_unknown_cwd_is_never_opaque(world):
    for text in ["cd $HOME && python3 -c 'x'", "cd $HOME && python3 s.py", "cd sub; python3 - <<'E'\nx\nE"]:
        seg = _seg(text, world["venue"]).segments[1]
        assert seg.status == "unresolved" and seg.opaque_kind is None, text


def test_an_unresolved_segment_has_no_venue(world):
    result = _seg("frobnicate x && cd sub && touch f", world["venue"])
    assert [(s.status, s.venue) for s in result.segments] == [
        ("unresolved", None),
        ("resolved", None),
        ("unresolved", None),
    ]


def test_resolved_segment_carries_the_cwd_it_ran_in(world):
    result = _seg("cd sub && touch f", world["venue"])
    assert [s.venue for s in result.segments] == [str(world["venue"]), str(world["venue"] / "sub")]


def test_detached_head_commit_is_unresolved(world):
    _git(world["venue"], "checkout", "-q", "--detach")
    assert _statuses(_seg("git commit -m x", world["venue"])) == ["unresolved"]
    assert _statuses(_seg("git push origin HEAD", world["venue"])) == ["unresolved"]


def test_outside_any_worktree_is_unresolved(world, tmp_path_factory):
    bare = tmp_path_factory.mktemp("bare")
    assert _statuses(_seg(f"git -C {bare} add -A", world["venue"])) == ["unresolved"]


def test_venue_that_is_not_a_repository_resolves_no_git_write(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _statuses(_seg("git add -A", plain)) == ["unresolved"]
    assert _statuses(_seg("touch f", plain)) == ["resolved"]


class TestGitAllowList:
    """Only the listed (subcommand, flag-shape) pairs resolve; the pin stops a widening from going unnoticed."""

    def test_the_allow_list_is_exactly_this(self):
        assert set(segment_resolver.GIT_WRITE_FORMS) == {
            "add", "commit", "restore", "rm", "mv", "merge", "checkout", "switch", "reset",
        }

    @pytest.mark.parametrize(
        "form",
        [
            "add a b", "add -A", "add -- a", "commit -q -m msg", "commit -a -m msg", "commit --message=msg",
            "commit -q -F -", "restore a", "restore --staged a", "rm a", "rm -r --cached a", "mv a b",
            "merge topic", "merge --no-ff -m msg topic", "checkout topic", "checkout -- a", "checkout HEAD -- a",
            "switch topic", "reset", "reset HEAD", "reset HEAD a", "reset -- a", "reset --mixed",
        ],
    )
    def test_listed_forms_resolve(self, form, world):
        assert _statuses(_seg(f"git {form}", world["venue"])) == ["resolved"], form

    @pytest.mark.parametrize(
        "form",
        [
            "stash", "stash push", "stash pop", "stash drop", "stash list", "stash apply",
            "rebase main", "rebase -i HEAD~3", "pull", "pull origin main", "am x.patch", "cherry-pick abc", "revert abc",
            "checkout -b t", "checkout -B t", "checkout -c t", "checkout -C t", "checkout --orphan t",
            "checkout --force t", "checkout --detach", "checkout --track origin/x", "checkout t u", "checkout",
            "switch -c t", "switch -C t", "switch --create t", "switch --force-create t", "switch --orphan t",
            "switch --detach t", "switch",
            "reset --soft HEAD~1", "reset --hard", "reset --hard HEAD~1", "reset --keep", "reset --merge",
            "reset HEAD~3", "reset abc123", "reset a",
            "commit --amend", "commit --amend -m x", "commit -m x --no-edit --amend", "commit --fixup abc",
            "commit -C abc", "commit -c abc", "commit --reuse-message=abc", "commit --template t",
            "add -p", "add -i", "add -e", "add --chmod=+x a", "add -f ignored",
            "rm -f a", "rm --force a", "rm", "mv a", "mv -f a b", "restore", "restore --source=abc a", "restore -p",
            "clean -fd", "clean -fdx", "gc", "prune", "reflog expire --all", "update-ref -d refs/heads/x",
            "symbolic-ref HEAD refs/heads/x", "branch -D x", "branch -m x y", "branch x", "tag x", "tag -d x",
            "config user.name x", "remote add o u", "remote set-url o u", "fetch", "fetch origin x:y", "clone u d",
            "init", "worktree add ../w", "worktree remove w", "apply p.patch", "filter-branch", "notes add",
            "merge --abort", "merge --continue", "merge", "submodule update", "bisect start", "format-patch HEAD~1",
            "push --force origin t", "push origin +t", "push origin :t", "push --delete origin t", "push",
            "push origin", "push origin --mirror", "push -u origin t", "push --tags origin t",
        ],
    )
    def test_forms_outside_the_list_are_unresolved(self, form, world):
        assert _statuses(_seg(f"git {form}", world["venue"])) == ["unresolved"], form

    @pytest.mark.parametrize("flag", ["-c x=y", "--exec-path=/x", "--namespace=n", "-p"])
    def test_unreviewed_global_flags_are_unresolved(self, flag, world):
        assert _statuses(_seg(f"git {flag} add -A", world["venue"])) == ["unresolved"], flag

    @pytest.mark.parametrize("sub", ["add $F", "add *.py", "add a/*", "add ~", "restore {a,b}", "rm $(ls)"])
    def test_non_literal_operands_are_unresolved(self, sub, world):
        assert _statuses(_seg(f"git {sub}", world["venue"])) == ["unresolved"], sub

    def test_a_branch_that_exists_only_in_packed_refs_counts_as_local(self, world):
        venue = world["venue"]
        _git(venue, "branch", "packed-only")
        _git(venue, "pack-refs", "--all")
        try:
            assert not (venue / ".git" / "refs" / "heads" / "packed-only").exists()
            assert _seg("git checkout packed-only", venue).resources == [_file(venue)]
        finally:
            _git(venue, "branch", "-q", "-D", "packed-only")

    def test_a_tag_that_exists_only_in_packed_refs_blocks_a_branch_push_source(self, world):
        venue = world["venue"]
        _git(venue, "branch", "packed-both")
        _git(venue, "tag", "packed-both")
        _git(venue, "pack-refs", "--all")
        try:
            assert not (venue / ".git" / "refs" / "tags" / "packed-both").exists()
            assert _statuses(_seg("git push origin packed-both:topic", venue)) == ["unresolved"]
        finally:
            _git(venue, "tag", "-d", "packed-both")
            _git(venue, "branch", "-q", "-D", "packed-both")

    @pytest.mark.parametrize(
        "text, cls",
        [
            ("git push origin v1:topic", "contract-unresolved"),
            ("git checkout nosuchbranch", "contract-unresolved"),
            ("git push origin feature:HEAD", "contract-unresolved"),
            ("git reset --hard", "contract-unresolved"),
            ("git add $F", "residual-syntax"),
            ("git clean -fd", "contract-unresolved"),
        ],
    )
    def test_git_refusals_carry_the_class_chosen_for_them(self, text, cls, world):
        seg = _seg(text, world["venue"]).segments[0]
        assert (seg.status, seg.reason_class) == ("unresolved", cls), text

    def test_read_only_git_has_no_effect(self, world):
        for text in ["git status --porcelain", "git log --oneline -5", "git diff a.py", "git rev-parse --short HEAD"]:
            result = _seg(text, world["venue"])
            assert _statuses(result) == ["resolved"] and result.resources == [], text

    def test_push_literal_resolves_to_a_push_resource(self, world):
        assert _seg("git push origin topic", world["venue"]).resources == [resources.VcsRefResource("origin", "topic", "push")]
        assert _seg("git push -q origin HEAD:refs/heads/main", world["venue"]).resources == [resources.VcsRefResource("origin", "main", "push")]

    def test_a_bare_push_destination_can_be_an_existing_non_branch_ref_on_the_remote(self, world, tmp_path):
        venue = world["venue"]
        bare = tmp_path / "bare.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
        _git(venue, "remote", "add", "holder", str(bare))
        try:
            _git(venue, "push", "-q", "holder", "topic:refs/remotes/other")
            _git(venue, "commit", "-q", "--allow-empty", "-m", "newer")
            _git(venue, "push", "-q", "holder", "feature:other")
            remote_refs = subprocess.run(["git", "-C", str(bare), "for-each-ref", "--format=%(refname)"], check=True, capture_output=True, text=True).stdout.split()
            assert remote_refs == ["refs/remotes/other"], remote_refs
            assert _statuses(_seg("git push holder feature:other", venue)) == ["unresolved"]
            assert _seg("git push holder feature:refs/heads/other", venue).resources == [resources.VcsRefResource("holder", "other", "push")]
        finally:
            _git(venue, "remote", "remove", "holder")
            _git(venue, "reset", "-q", "--hard", "HEAD~1")

    def test_dash_c_inside_the_venue_equals_no_dash_c(self, world):
        venue = world["venue"]
        assert _seg(f"git -C {venue} add -A", venue).resources == _seg("git add -A", venue).resources
        assert _seg(f"git -C {venue}/sub add x", venue).resources == [_file(venue)]
        assert _seg("git -C sub add x", venue).resources == [_file(venue)]

    def test_git_follows_a_rebased_cwd_inside_the_repo(self, world):
        result = _seg("cd sub && git commit -q -m x", world["venue"])
        assert set(result.segments[1].resources) == {_file(world["venue"]), resources.VcsRefResource(".", "feature", "commit")}


class TestOperators:
    @pytest.mark.parametrize("op", ["&&", ";", "|", "||", "&", "\n", "|&"])
    def test_each_operator_splits_into_two_segments(self, op, world):
        result = _seg(f"echo a {op} echo b", world["venue"])
        assert [s.text for s in result.segments] == ["echo a", "echo b"]
        assert _statuses(result) == ["resolved", "resolved"]

    def test_the_separators_are_not_part_of_the_segment_text(self, world):
        result = _seg("touch /a ; touch /b && touch /c", world["venue"])
        assert [s.text for s in result.segments] == ["touch /a", "touch /b", "touch /c"]
        assert [r.path for s in result.segments for r in s.resources] == ["/a", "/b", "/c"]

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
        assert result.segments[2].text == "echo done"

    @pytest.mark.parametrize("text", ["echo $(date)", "echo `date`", "(cd /; ls)", "for i in 1; do ls; done", "echo <(ls)"])
    def test_nested_execution_is_unresolved(self, text, world):
        assert "unresolved" in _statuses(_seg(text, world["venue"]))

    @pytest.mark.parametrize("text", ['echo "oops && ls', "echo 'oops", "echo `oops", "echo $(oops"])
    def test_unterminated_construct_is_one_unresolved_segment(self, text, world):
        result = _seg(text, world["venue"])
        assert _statuses(result) == ["unresolved"]
        assert result.segments[0].reason_class == "residual-syntax"

    def test_one_undecidable_segment_does_not_hide_the_others(self, world):
        result = _seg("touch a.txt && frobnicate x; touch /abs/b", world["venue"])
        assert _statuses(result) == ["resolved", "unresolved", "resolved"]
        assert result.segments[0].resources == [_file(world["venue"] / "a.txt")]
        assert result.segments[1].reason_class == "unknown-program"

    @pytest.mark.parametrize(
        "text, cls",
        [("frobnicate x", "unknown-program"), ("FOO=1 ls", "contract-unresolved"), ("env ls", "contract-unresolved"), ("/bin/ls", "contract-unresolved")],
    )
    def test_unknown_program_assignment_wrapper_and_path_are_unresolved(self, text, cls, world):
        seg = _seg(text, world["venue"]).segments[0]
        assert (seg.status, seg.reason_class) == ("unresolved", cls), text


class TestCd:
    def test_cd_chain_accumulates(self, world):
        result = _seg("cd sub && cd .. && touch f", world["venue"])
        assert result.segments[2].resources == [_file(world["venue"] / "f")]

    def test_absolute_cd_then_relative_write(self, world):
        result = _seg(f"cd {world['outside']} && touch r", world["venue"])
        assert result.segments[1].resources == [_file(world["outside"] / "r")]

    def test_absolute_segment_after_semicolon_following_a_cd_still_resolves(self, world):
        result = _seg(f"cd sub; touch {world['outside']}/abs", world["venue"])
        assert result.segments[1].resources == [_file(world["outside"] / "abs")]

    def test_semicolon_without_any_cd_keeps_the_venue(self, world):
        result = _seg("touch a; touch b", world["venue"])
        assert result.segments[1].resources == [_file(world["venue"] / "b")]

    def test_cd_after_a_pipeline_member_only_moves_the_subshell(self, world):
        result = _seg("cd sub && cat f | touch g", world["venue"])
        assert result.segments[2].resources == [_file(world["venue"] / "sub" / "g")]

    @pytest.mark.parametrize("cd", ["cd $HOME", "cd -", "cd", "cd ~", "cd sub/*", "cd -P sub", "cd -L sub"])
    def test_unreadable_cd_target_makes_later_relative_segments_unresolved(self, cd, world):
        assert _seg(f"{cd} && touch f", world["venue"]).segments[-1].status == "unresolved", cd

    @pytest.mark.parametrize("text", ["echo $(date) && touch f", "pushd sub && touch f", "frobnicate && touch f", "FOO=1 ls && touch f"])
    def test_an_unreadable_segment_poisons_the_cwd(self, text, world):
        assert _seg(text, world["venue"]).segments[-1].status == "unresolved", text

    def test_an_unreadable_segment_poisons_the_cwd_across_semicolons(self, world):
        assert _seg("frobnicate; touch f", world["venue"]).segments[-1].status == "unresolved"
        assert _seg("frobnicate; touch /abs", world["venue"]).segments[-1].status == "resolved"

    @pytest.mark.parametrize("operand", ["sub", "sub/deep", "lnk"])
    def test_cdpath_makes_a_bare_operand_unresolvable(self, operand, world, monkeypatch):
        monkeypatch.setenv("CDPATH", "/somewhere")
        assert _seg(f"cd {operand} && touch f", world["venue"]).segments[-1].status == "unresolved", operand

    @pytest.mark.parametrize("operand", ["./sub", "../venue/sub", ".", ".."])
    def test_cdpath_does_not_apply_to_explicitly_relative_operands(self, operand, world, monkeypatch):
        monkeypatch.setenv("CDPATH", "/somewhere")
        assert _seg(f"cd {operand} && touch f", world["venue"]).segments[-1].status == "resolved", operand

    def test_cdpath_does_not_apply_to_an_absolute_operand(self, world, monkeypatch):
        monkeypatch.setenv("CDPATH", "/somewhere")
        assert _seg(f"cd {world['venue']}/sub && touch f", world["venue"]).segments[-1].resources == [_file(world["venue"] / "sub" / "f")]

    def test_an_empty_cdpath_is_no_cdpath(self, world, monkeypatch):
        monkeypatch.setenv("CDPATH", "")
        assert _seg("cd sub && touch f", world["venue"]).segments[-1].resources == [_file(world["venue"] / "sub" / "f")]


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


class TestOtherPrograms:
    def test_mkdir_and_touch_write_each_operand(self, world):
        assert _seg("mkdir -p a/b c", world["venue"]).resources == [_file(world["venue"] / "a"), _file(world["venue"] / "c")]
        assert _seg("touch x y", world["venue"]).resources == [_file(world["venue"] / "x"), _file(world["venue"] / "y")]

    def test_mv_writes_all_operands_and_cp_only_the_destination(self, world):
        assert set(_seg("mv a b", world["venue"]).resources) == {_file(world["venue"] / "a"), _file(world["venue"] / "b")}
        assert _seg("cp a b", world["venue"]).resources == [_file(world["venue"] / "b")]

    @pytest.mark.parametrize("text", ["mv -t dir a", "cp --target-directory=d a", "mv a", "touch -r ref f", "mkdir -m 777 d", "mv a $B"])
    def test_unreviewed_flags_and_shapes_are_unresolved(self, text, world):
        assert _statuses(_seg(text, world["venue"])) == ["unresolved"], text

    def test_awk_print_program_has_no_effect(self, world):
        result = _seg("awk -F'\\t' '{print $1,$3}' data.tsv", world["venue"])
        assert _statuses(result) == ["resolved"] and result.resources == []

    @pytest.mark.parametrize("prog", ["{print > \"x\"}", "{print | \"sh\"}", "BEGIN{system(\"ls\")}", "{\"ls\" | getline}"])
    def test_awk_program_that_redirects_or_executes_is_unresolved(self, prog, world):
        assert _statuses(_seg(f"awk '{prog}' f", world["venue"])) == ["unresolved"], prog

    @pytest.mark.parametrize(
        "text, kind",
        [
            ("python3 -c 'print(1)'", "python-c"),
            ("python3 -W ignore -c 'print(1)'", "python-c"),
            ("python3 - <<'EOF'\nprint(1)\nEOF", "stdin-body"),
            ("python3 <<'EOF'\nprint(1)\nEOF", "stdin-body"),
            ("bash <<'EOF'\nls\nEOF", "stdin-body"),
            ("sh -s <<'EOF'\nls\nEOF", "stdin-body"),
            ("python3 some_script.py", "script-file"),
            ("bash run.sh", "script-file"),
        ],
    )
    def test_interpreters_with_unreadable_code_are_opaque(self, text, kind, world):
        seg = _seg(text, world["venue"]).segments[0]
        assert (seg.status, seg.opaque_kind) == ("opaque", kind)

    @pytest.mark.parametrize("text", ["bash -c 'ls'", "sh -ec 'ls'", "bash -lc ls", "node -e 'x'", "perl -e 'x'", "python3 -m http.server"])
    def test_inline_code_other_than_python_c_is_unresolved(self, text, world):
        assert _statuses(_seg(text, world["venue"])) == ["unresolved"], text

    def test_python_m_pytest_goes_through_the_registry_resolver(self, world):
        seg = _seg("python3 -m pytest -q scripts/tests/test_x.py", world["venue"]).segments[0]
        assert seg.status == "resolved" and seg.resources == [_file(world["venue"])]

    def test_table_unresolved_program_stays_unresolved(self, world):
        seg = _seg("rm -rf x", world["venue"]).segments[0]
        assert (seg.status, seg.reason_class) == ("unresolved", "contract-unresolved")

    def test_none_effect_program_with_unreviewed_flag_is_unresolved(self, world):
        table = tool_contracts.load_contract_table()
        assert _seg("rg --pre sh pattern", world["venue"], contract_table=table).segments[0].status == "unresolved"


class TestNonLiteralCode:
    @pytest.mark.parametrize(
        "text",
        ["awk \"$P\" f", 'python3 -c "$CODE"', "python3 $S", "bash $S", "node --eval=x", "perl -ne 'print' f", "echo x | python3"],
    )
    def test_code_the_resolver_cannot_read_is_unresolved_not_opaque(self, text, world):
        seg = _seg(text, world["venue"]).segments[-1]
        assert seg.status == "unresolved" and seg.opaque_kind is None, text

    @pytest.mark.parametrize("text", ["awk \"$P\" f", 'python3 -c "$CODE"', "python3 $S"])
    def test_nonliteral_operands_are_classed_as_residual_syntax(self, text, world):
        assert _seg(text, world["venue"]).segments[0].reason_class == "residual-syntax", text


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

    def test_the_same_line_resolves_in_segment_mode_only(self, world):
        text = f"git -C {world['venue']} add -A && echo hi > out.txt"
        assert tool_contracts.resolve_command(text, str(world["venue"])).status == "unresolved"
        assert _statuses(_seg(text, world["venue"])) == ["resolved", "resolved"]

    def test_segment_mode_leaves_resolve_command_results_identical(self, world):
        before = {t: tool_contracts.resolve_command(t, str(world["venue"])) for t in ["git status", "git push origin x", "cat a | head"]}
        for t in before:
            _seg(t, world["venue"])
        assert before == {t: tool_contracts.resolve_command(t, str(world["venue"])) for t in before}


def print_probes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        for probe_id, template, _ in PROBES:
            command = _subst(template, world)
            result = _seg(command, world["venue"])
            print(f"\n[{probe_id}] {command!r}")
            for seg in result.segments:
                shown = [_norm(r) for r in seg.resources]
                print(f"  [{seg.status}] {seg.text!r} resources={shown} opaque_kind={seg.opaque_kind} reason_class={seg.reason_class}")


if __name__ == "__main__":
    print_probes()
