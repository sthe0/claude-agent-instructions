"""Regression tests for the stash lifecycle of sync-instructions-repo.sh.

Guards four defects that together let a `pull` inject a foreign session's work
into the canonical checkout and leave it conflicted, silently:

* an inverted `has_uncommitted` that made every pull stash and pop;
* a `pop_stash_if_any` that tested only "the stash stack is non-empty" and ran a
  bare `git stash pop`, i.e. restored whatever entry happened to be on top;
* a failing pop that left unmerged paths and conflict markers in the tree;
* a failure that reached neither stderr nor the exit status.

The script under test comes from the SYNC_SCRIPT environment variable so
prove_stash_tests_discriminate.sh can point these same tests at the pre-fix
script (and at two half-fixed surrogates) and show that they go red there.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from test_sync_instructions_repo import GIT_ENV, git

DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent / "sync-instructions-repo.sh"
SCRIPT = Path(os.environ.get("SYNC_SCRIPT") or DEFAULT_SCRIPT)

FOREIGN_LABEL = "rescue: foreign session"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def make_repo(tmp_path: Path, name: str):
    """Bare 'origin' seeded with one commit on main, plus a clone of it."""
    origin = tmp_path / f"{name}.git"
    git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    seed = tmp_path / f"{name}-seed"
    git("clone", "--quiet", str(origin), str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("seed\n")
    git("add", "README.md", cwd=seed)
    git("commit", "--quiet", "-m", "seed: initial content", cwd=seed)
    git("push", "--quiet", "origin", "main", cwd=seed)

    clone = tmp_path / f"{name}-clone"
    git("clone", "--quiet", str(origin), str(clone), cwd=tmp_path)
    return origin, clone


def push_upstream(tmp_path: Path, origin: Path, name: str, path: str, content: str):
    """Push one commit to origin/main that writes `content` into `path`."""
    other = tmp_path / f"{name}-upstream"
    git("clone", "--quiet", "-b", "main", str(origin), str(other), cwd=tmp_path)
    (other / path).write_text(content)
    git("add", path, cwd=other)
    git("commit", "--quiet", "-m", f"upstream: {path}", cwd=other)
    git("push", "--quiet", "origin", "main", cwd=other)


def run_script(repo: Path, cmd, home: Path, script: Path = None, extra_env: dict = None):
    argv = ["bash", str(script or SCRIPT)] + ([cmd] if cmd else [])
    env = {
        **os.environ,
        **GIT_ENV,
        "HOME": str(home),
        "CLAUDE_INSTRUCTIONS_REPO": str(repo),
        **(extra_env or {}),
    }
    return subprocess.run(argv, env=env, capture_output=True, text=True)


def stash_entries(repo: Path):
    """[(sha, subject)] for the whole stack, top first."""
    out = git("stash", "list", "--format=%H%x09%s", cwd=repo).stdout.splitlines()
    return [tuple(line.split("\t", 1)) for line in out if line]


def make_foreign_stash(repo: Path) -> str:
    """Park an unrelated session's work on the stack; return its sha."""
    (repo / "foreign-work.txt").write_text("another session's unfinished work\n")
    git("stash", "push", "-u", "--quiet", "-m", FOREIGN_LABEL, cwd=repo)
    return git("rev-parse", "stash@{0}", cwd=repo).stdout.strip()


def porcelain(repo: Path) -> str:
    return git("status", "--porcelain", cwd=repo).stdout.strip()


def unmerged(repo: Path) -> str:
    return git("ls-files", "--unmerged", cwd=repo).stdout.strip()


def test_clean_tree_pull_leaves_a_foreign_stash_alone(tmp_path, home):
    """(a) A clean tree must stash nothing and therefore pop nothing — the
    foreign entry on the stack is not this run's to restore."""
    origin, clone = make_repo(tmp_path, "a")
    foreign = make_foreign_stash(clone)
    assert porcelain(clone) == "", "precondition: the tree is clean after stashing"
    push_upstream(tmp_path, origin, "a", "ADVANCE.md", "advance\n")

    result = run_script(clone, "pull", home)

    assert result.returncode == 0, f"clean pull must succeed: {result.stderr}"
    entries = stash_entries(clone)
    assert [e[0] for e in entries] == [foreign], (
        f"the foreign stash must survive untouched, stack is {entries}"
    )
    assert porcelain(clone) == "", (
        f"a clean tree must stay clean — foreign work was injected: {porcelain(clone)}"
    )
    assert (clone / "ADVANCE.md").exists(), "the incoming commit must be applied"


def test_a_relapsed_predicate_still_cannot_consume_a_foreign_stash(tmp_path, home):
    """(e) Defence in depth: ONE boolean regressing must not be enough to destroy
    foreign work.

    `has_uncommitted` is patched back to the identically-true predicate that was
    defect D0 — the exact relapse a future edit could reintroduce. Nothing else is
    touched. The remaining guards must still hold the line: the push creates no
    entry, so before == after and stash_if_dirty refuses right there — the
    run-label check is never even reached — SYNC_STASH_SHA stays empty and no pop
    is attempted.

    Deliberately asserts NON-DESTRUCTION only, not the exit status. The fixed
    script refuses (non-zero + a loud WARN) and the entry-appeared surrogate of
    prove_stash_tests_discriminate.sh continues (zero); both are safe, and
    loudness is already pinned by tests (c), (c') and (d). Asserting it here would
    only collapse the surrogate separation that wrapper depends on.
    """
    relapsed = tmp_path / "relapsed-sync.sh"
    patched, n = re.subn(
        r"has_uncommitted\(\) \{.*?\n\}",
        "has_uncommitted() {\n  return 0\n}",
        SCRIPT.read_text(),
        flags=re.S,
    )
    assert n == 1, (
        "the has_uncommitted relapse did not apply — an unpatched copy would make "
        "this test a tautology"
    )
    relapsed.write_text(patched)

    origin, clone = make_repo(tmp_path, "e")
    foreign = make_foreign_stash(clone)
    assert porcelain(clone) == "", "precondition: the tree is clean after stashing"
    push_upstream(tmp_path, origin, "e", "ADVANCE.md", "advance\n")
    before = stash_entries(clone)

    result = run_script(clone, "pull", home, script=relapsed)

    # Non-destruction alone cannot tell "the guards held" from "the copy never
    # ran at all" — a truncated script passes every assertion below. This marker
    # is emitted by cmd_pull in every variant the wrapper builds, base included,
    # so requiring it costs no discrimination.
    assert "pull start" in result.stdout, (
        f"the relapsed copy never reached cmd_pull, so this test proves nothing: "
        f"{result.stdout!r} / {result.stderr!r}"
    )
    assert stash_entries(clone) == before, (
        f"a relapsed predicate consumed the stash stack: {before} -> "
        f"{stash_entries(clone)}"
    )
    assert [sha for sha, _ in stash_entries(clone)] == [foreign], (
        "the foreign entry must still be at its own sha"
    )
    assert not (clone / "foreign-work.txt").exists(), (
        "another session's work was popped into the tree"
    )
    assert porcelain(clone) == "", (
        f"a clean tree must stay clean, found: {porcelain(clone)!r}"
    )


def _install_foreign_stash_hook(repo: Path, marker: Path, hook_name: str = "post-merge"):
    """A hook that pushes a FOREIGN stash on top of ours — the one way a second
    process can slip an entry between our push and our pop.

    `exit 0` is explicit because `pre-rebase` ABORTS the rebase on a non-zero
    status, which would silently turn its test into a different scenario.
    """
    hook = repo / ".git" / "hooks" / hook_name
    hook.write_text(
        "#!/bin/sh\n"
        f'[ -f "{marker}" ] && exit 0\n'
        "unset GIT_INDEX_FILE\n"
        "echo interloper > foreign-work.txt\n"
        f'git stash push -u -q -m "{FOREIGN_LABEL}" || exit 0\n'
        f'touch "{marker}"\n'
        "exit 0\n"
    )
    hook.chmod(0o755)


def test_pull_restores_our_stash_not_the_one_on_top(tmp_path, home):
    """(b) With a foreign entry landing on top mid-pull, the pull must restore
    OUR entry by identity and leave the foreign one on the stack."""
    origin, clone = make_repo(tmp_path, "b")
    (clone / "README.md").write_text("seed\nlocal session work\n")
    (clone / "local-work.txt").write_text("untracked local work\n")
    marker = tmp_path / "post-merge.fired"
    _install_foreign_stash_hook(clone, marker)
    push_upstream(tmp_path, origin, "b", "ADVANCE.md", "advance\n")

    result = run_script(clone, "pull", home)

    assert marker.exists(), (
        "the post-merge fixture never fired — without a foreign entry arriving "
        "mid-pull this test proves nothing"
    )
    assert result.returncode == 0, f"pull should succeed: {result.stderr}"
    assert (clone / "README.md").read_text() == "seed\nlocal session work\n", (
        "our tracked local change was not restored"
    )
    assert (clone / "local-work.txt").exists(), (
        "our untracked local file was not restored"
    )
    subjects = [s for _, s in stash_entries(clone)]
    assert len(subjects) == 1 and FOREIGN_LABEL in subjects[0], (
        f"exactly the foreign entry must remain on the stack, got {subjects}"
    )


def test_pull_restores_our_stash_on_the_rebase_path_too(tmp_path, home):
    """(b') The rebase twin of (b).

    `post-merge` fires only on the ff-merge branch cmd_pull takes when ahead == 0.
    With local commits ahead the pull rebases instead and that hook never runs, so
    the rebase branch had no concurrent-writer coverage at all. `pre-rebase` is the
    equivalent seam there.
    """
    origin, clone = make_repo(tmp_path, "brebase")
    # A local commit is what pushes cmd_pull down the `ahead > 0` rebase branch.
    (clone / "local-commit.txt").write_text("committed local work\n")
    git("add", "local-commit.txt", cwd=clone)
    git("commit", "--quiet", "-m", "local: ahead of origin", cwd=clone)

    (clone / "README.md").write_text("seed\nlocal session work\n")
    (clone / "local-work.txt").write_text("untracked local work\n")
    marker = tmp_path / "pre-rebase.fired"
    _install_foreign_stash_hook(clone, marker, "pre-rebase")
    push_upstream(tmp_path, origin, "brebase", "ADVANCE.md", "advance\n")

    result = run_script(clone, "pull", home)

    # The marker is also how we know the rebase branch was really taken: pre-rebase
    # cannot fire on the ff-merge path.
    assert marker.exists(), (
        "the pre-rebase fixture never fired — either the pull did not rebase or no "
        "foreign entry arrived mid-pull, and either way this test proves nothing"
    )
    assert result.returncode == 0, f"pull should succeed: {result.stderr}"
    assert (clone / "README.md").read_text() == "seed\nlocal session work\n", (
        "our tracked local change was not restored"
    )
    assert (clone / "local-work.txt").exists(), (
        "our untracked local file was not restored"
    )
    subjects = [s for _, s in stash_entries(clone)]
    assert len(subjects) == 1 and FOREIGN_LABEL in subjects[0], (
        f"exactly the foreign entry must remain on the stack, got {subjects}"
    )


def _install_stash_pop_race_wrapper(bin_dir: Path, marker: Path):
    """A `git` wrapper placed first on PATH that, the first time it is invoked
    as exactly `stash pop <ref>`, pushes a FOREIGN entry (shifting every
    index) BEFORE delegating to the real pop — reproducing the positional
    race that a concurrent `git stash push` could still open, even with the
    window between ref resolution and the pop narrowed to zero of *our own*
    instructions: `git stash pop <ref>` is itself a single command, and
    another process can still land its push while that one command runs.

    Resolves the real git path before the wrapper directory ever enters PATH
    (`shutil.which` below, from the test process's own unpatched PATH) so the
    wrapper never resolves itself. Compares the subcommand pair by exact
    equality and uses a one-shot latch, because the script also calls `git
    rebase --continue` / `--abort`, which an unlatched or substring-matching
    wrapper would also catch.
    """
    import shutil

    real_git = shutil.which("git")
    assert real_git, "git not found on PATH — cannot build the race wrapper"
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'REAL_GIT="{real_git}"\n'
        f'MARKER="{marker}"\n'
        f'FOREIGN_LABEL="{FOREIGN_LABEL}"\n'
        'if [ "$1" = "stash" ] && [ "$2" = "pop" ] && [ ! -f "$MARKER" ]; then\n'
        '  touch "$MARKER"\n'
        '  echo interloper > foreign-race-work.txt\n'
        '  "$REAL_GIT" stash push -u -q -m "$FOREIGN_LABEL" || true\n'
        "fi\n"
        'exec "$REAL_GIT" "$@"\n'
    )
    wrapper.chmod(0o755)


def test_pop_detects_a_stack_shift_and_refuses_instead_of_hiding_it(tmp_path, home):
    """(f) BLOCKING regression. Narrowing the window between ref resolution and
    the pop to zero of our own instructions is not enough: `git stash pop
    <ref>` is one command, and a concurrent `git stash push` landing while
    THAT command runs still shifts every index, so the positional pop can
    still consume a foreign entry instead of ours. The pop must detect this —
    our own sha must be gone from the stack afterward — and fail loudly
    rather than silently leave a different session's work in the tree."""
    origin, clone = make_repo(tmp_path, "f")
    (clone / "README.md").write_text("seed\nlocal session work\n")
    push_upstream(tmp_path, origin, "f", "ADVANCE.md", "advance\n")

    bin_dir = tmp_path / "f-bin"
    bin_dir.mkdir()
    marker = tmp_path / "f-race.fired"
    _install_stash_pop_race_wrapper(bin_dir, marker)

    result = run_script(
        clone, "pull", home,
        extra_env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert marker.exists(), (
        "the race wrapper never fired — this test proves nothing"
    )
    assert result.returncode != 0, (
        "a pull that silently popped a foreign entry instead of ours must fail"
    )
    entries = stash_entries(clone)
    ours = [sha for sha, subject in entries if "sync-instructions-repo" in subject]
    assert len(ours) == 1, (
        f"our own entry must survive the race on the stack, got {entries}"
    )
    assert ours[0] in result.stderr, (
        f"stderr must name our own sha ({ours[0]}) so 'stash apply' recovers it: "
        f"{result.stderr!r}"
    )
    assert "STILL on the stack" in result.stderr, (
        f"stderr must say the stack shifted under us, got: {result.stderr!r}"
    )


def _install_post_pop_stash_list_failure_wrapper(bin_dir: Path, pop_marker: Path, fail_marker: Path):
    """A `git` wrapper that runs `stash pop <ref>` for real, then makes the
    FIRST `stash list` call after that pop — and only that one, one-shot —
    exit non-zero. Reproduces a `git stash list` failure landing exactly
    inside the post-pop presence check `pop_stash_if_any` performs, and
    nowhere else: `stash_ref_for_sha` also calls `stash list` to resolve the
    ref BEFORE the pop, and that earlier call must go through untouched or the
    pop itself would never happen and this test would prove nothing.
    """
    import shutil

    real_git = shutil.which("git")
    assert real_git, "git not found on PATH — cannot build the wrapper"
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'REAL_GIT="{real_git}"\n'
        f'POP_MARKER="{pop_marker}"\n'
        f'FAIL_MARKER="{fail_marker}"\n'
        'if [ "$1" = "stash" ] && [ "$2" = "pop" ]; then\n'
        '  "$REAL_GIT" "$@"\n'
        '  rc=$?\n'
        '  touch "$POP_MARKER"\n'
        '  exit $rc\n'
        'fi\n'
        'if [ "$1" = "stash" ] && [ "$2" = "list" ] && [ -f "$POP_MARKER" ] && [ ! -f "$FAIL_MARKER" ]; then\n'
        '  touch "$FAIL_MARKER"\n'
        '  exit 1\n'
        'fi\n'
        'exec "$REAL_GIT" "$@"\n'
    )
    wrapper.chmod(0o755)


def test_pop_post_check_fails_loudly_when_stash_list_cannot_confirm_it(tmp_path, home):
    """(h) BLOCKING regression. `stash_ref_for_sha`'s enumeration failure must
    not be read, in the post-pop presence check, as "our entry is gone" — that
    reading is a CLEAN POP, the one fail-open direction left in a change whose
    entire purpose is to make a wrong outcome loud. A `git stash list` that
    fails right after a reported-successful pop must make the pull fail
    loudly instead of reporting success."""
    origin, clone = make_repo(tmp_path, "h")
    (clone / "README.md").write_text("seed\nlocal session work\n")
    push_upstream(tmp_path, origin, "h", "ADVANCE.md", "advance\n")

    bin_dir = tmp_path / "h-bin"
    bin_dir.mkdir()
    pop_marker = tmp_path / "h-pop.fired"
    fail_marker = tmp_path / "h-list-fail.fired"
    _install_post_pop_stash_list_failure_wrapper(bin_dir, pop_marker, fail_marker)

    result = run_script(
        clone, "pull", home,
        extra_env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert pop_marker.exists(), "the pop never happened — this test proves nothing"
    assert fail_marker.exists(), (
        "the post-pop stash-list latch never fired — this test proves nothing"
    )
    assert result.returncode != 0, (
        "a pull whose post-pop stash-list check failed must not report success"
    )
    assert "cannot tell" in result.stderr, (
        f"stderr must say the entry's survival could not be confirmed, got: {result.stderr!r}"
    )


def _install_merge_created_untracked_file_hook(repo: Path, path_name: str, content: str, hook_name: str = "post-merge"):
    """A hook that plants an untracked file at `path_name` right after the
    merge completes — simulating a file the merge itself produced, arriving
    before pop_stash_if_any takes its untracked_before snapshot and before the
    pop ever runs."""
    hook = repo / ".git" / "hooks" / hook_name
    hook.write_text(
        "#!/bin/sh\n"
        f'printf %s "{content}" > "{path_name}"\n'
        "exit 0\n"
    )
    hook.chmod(0o755)


def test_conflicting_pop_preserves_a_merge_created_non_ascii_untracked_file(tmp_path, home):
    """(h') should-fix regression, the third quotePath source. `pop_stash_if_any`
    snapshots untracked files into $untracked_before BEFORE resolving the
    stash ref and popping; that snapshot must be taken with the same
    core.quotePath=false as $now/$stash_untracked inside restore_after_failed_pop,
    or a non-ASCII path present in BOTH — created by the merge itself, and
    coincidentally also part of our own stashed untracked tree — fails the
    "was it already here?" comparison and gets deleted by the residue sweep
    instead of left alone."""
    origin, clone = make_repo(tmp_path, "h2")
    non_ascii_name = "café-résumé.txt"
    (clone / non_ascii_name).write_text("local stash content\n")
    _install_merge_created_untracked_file_hook(clone, non_ascii_name, "merge-created content\n")
    push_upstream(tmp_path, origin, "h2", "ADVANCE.md", "advance\n")

    result = run_script(clone, "pull", home)

    assert result.returncode != 0, (
        "the untracked-file collision must make the pop conflict and the pull fail"
    )
    assert (clone / non_ascii_name).exists(), (
        "the file present before the pop was attempted must not be deleted by the "
        "residue sweep"
    )
    assert (clone / non_ascii_name).read_text() == "merge-created content\n", (
        "the merge-created content must survive untouched, not be overwritten or "
        "removed by the sweep"
    )
    entries = stash_entries(clone)
    assert len(entries) == 1 and "sync-instructions-repo" in entries[0][1], (
        f"our stash must be preserved, stack is {entries}"
    )
    assert "stash apply" in result.stderr, (
        f"stderr must name the recovery command, got: {result.stderr!r}"
    )


def _conflicting_pull_setup(tmp_path: Path, name: str):
    origin, clone = make_repo(tmp_path, name)
    (clone / "README.md").write_text("local edit\n")
    (clone / "local-untracked.txt").write_text("untracked local work\n")
    push_upstream(tmp_path, origin, name, "README.md", "upstream edit\n")
    return origin, clone


def test_conflicting_pop_restores_the_tree_and_reports_loudly(tmp_path, home):
    """(c) A pop that cannot apply must leave a clean tree at origin/main, keep
    our work in the stash, name the recovery command on stderr, and fail."""
    origin, clone = _conflicting_pull_setup(tmp_path, "c")

    result = run_script(clone, "pull", home)

    assert result.returncode != 0, "a pull that could not restore session work must fail"
    assert porcelain(clone) == "", (
        f"the tree must be restored to HEAD, found: {porcelain(clone)!r}"
    )
    assert unmerged(clone) == "", "no unmerged paths may be left behind"
    assert (clone / "README.md").read_text() == "upstream edit\n"
    assert not (clone / "local-untracked.txt").exists(), (
        "untracked residue from the failed pop must be removed, or the advertised "
        "'stash apply' recovery fails with 'already exists'"
    )
    entries = stash_entries(clone)
    assert len(entries) == 1 and "sync-instructions-repo" in entries[0][1], (
        f"our stash must be preserved, stack is {entries}"
    )
    assert "stash apply" in result.stderr, (
        f"stderr must name the recovery command, got: {result.stderr!r}"
    )


def test_conflicting_pop_removes_non_ascii_untracked_residue(tmp_path, home):
    """(c') should-fix regression. Git C-quotes any path with non-ASCII bytes
    (or an embedded quote/backslash/tab/newline) by default, so a plain `[[ -f
    "$path" ]]` comparison against unadorned `git ls-files`/`git ls-tree`
    output never matches it: the residue survives, and the `stash apply`
    recovery the script advertises in the same breath then fails with
    'already exists'."""
    origin, clone = _conflicting_pull_setup(tmp_path, "c2")
    non_ascii_name = "café-résumé.txt"
    (clone / non_ascii_name).write_text("untracked local work with accents\n")

    result = run_script(clone, "pull", home)

    assert result.returncode != 0, "a pull that could not restore session work must fail"
    assert not (clone / non_ascii_name).exists(), (
        "non-ASCII untracked residue from the failed pop must be removed"
    )
    entries = stash_entries(clone)
    assert len(entries) == 1 and "sync-instructions-repo" in entries[0][1], (
        f"our stash must be preserved, stack is {entries}"
    )
    sha = entries[0][0]
    apply_result = git("stash", "apply", sha, cwd=clone, check=False)
    # This fixture's stash also carries a genuine, unrelated README.md content
    # conflict (from _conflicting_pull_setup), so the FULL apply legitimately
    # cannot return 0 here regardless of the quoting fix — pin the should-fix
    # bug specifically: the non-ASCII path must no longer collide with itself
    # ("already exists" is the exact failure mode C-quoting caused), and its
    # own untracked content must come back correctly.
    combined_output = apply_result.stdout + apply_result.stderr
    assert "already exists" not in combined_output, (
        f"stash apply must not fail on the non-ASCII path's own residue: "
        f"{apply_result.stdout!r} {apply_result.stderr!r}"
    )
    assert (clone / non_ascii_name).read_text() == "untracked local work with accents\n", (
        "stash apply must restore the non-ASCII file's own content"
    )


def test_conflicting_pop_also_fails_the_default_sync_entry_point(tmp_path, home):
    """(c') `sync` is the DEFAULT subcommand; it must not swallow the failure."""
    origin, clone = _conflicting_pull_setup(tmp_path, "csync")

    result = run_script(clone, None, home)

    assert result.returncode != 0, (
        "the default entry point swallowed a pull that left session work un-restored"
    )
    assert "stash apply" in result.stderr


def test_pull_refuses_on_an_already_unmerged_tree(tmp_path, home):
    """(d) An unmerged index is the state where `stash push` saves nothing while
    a positional pop restores someone else's entry. Refuse, loudly."""
    origin, clone = make_repo(tmp_path, "d")
    foreign = make_foreign_stash(clone)

    git("checkout", "--quiet", "-b", "side", cwd=clone)
    (clone / "README.md").write_text("side\n")
    git("commit", "--quiet", "-am", "side: edit", cwd=clone)
    git("checkout", "--quiet", "main", cwd=clone)
    (clone / "README.md").write_text("mine\n")
    git("commit", "--quiet", "-am", "main: edit", cwd=clone)
    merge = git("merge", "side", cwd=clone, check=False)
    assert merge.returncode != 0 and unmerged(clone), "precondition: unmerged paths"

    push_upstream(tmp_path, origin, "d", "ADVANCE.md", "advance\n")
    before = stash_entries(clone)

    result = run_script(clone, "pull", home)

    assert result.returncode != 0, "a pull on an unmerged tree must fail"
    assert "REFUSED" in result.stderr, f"stderr must say REFUSED, got: {result.stderr!r}"
    after = stash_entries(clone)
    assert after == before and [sha for sha, _ in after] == [foreign], (
        f"the stash stack must be untouched, got {after}"
    )
    assert unmerged(clone), "the unmerged paths must be left for the human to resolve"
