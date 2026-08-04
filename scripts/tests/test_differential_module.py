"""Real-git tests for agentctl/differential.py — Stage 2 of the
differential-verify plan.

Twenty-five cases across base resolution (A2), the multiset comparison (A3),
the empty-evidence guard (A3'), and the refusal taxonomy (A5). The git-plumbing
cases (base resolution, rebase, degenerate/frozen, the violation
comparisons) run against REAL temporary git repositories with the real
subprocess runner, so actual git semantics are exercised rather than mocked;
the refusal cases that need a git command to fail on cue use an injected
fake runner instead. Schema/validation (D1-D7) is Stage 1's
test_differential_schema.py, not repeated here.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import tempfile

import pytest

from agentctl import differential
from agentctl.dispatch import RunResult, subprocess_runner
from agentctl.state import DifferentialSpec, SessionState

# --- real-git fixture helpers ------------------------------------------------


def _git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _init_repo(tmp_path):
    """A bare `origin` remote plus a `work` clone, with one commit on `main`
    pushed to origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    _git(work, "checkout", "-q", "-B", "main")
    (work / "keep.txt").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "T0")
    _git(work, "push", "-q", "origin", "main")
    return work, origin


def _commit(work, filename, content, message):
    (work / filename).write_text(content)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)


def _state():
    return SessionState(session_id="s", task_id="t")


class _RecordingRunner:
    """Wraps the real subprocess_runner, recording every argv issued — used
    to prove the no-network invariant (no fetch/ls-remote/pull)."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        return subprocess_runner(argv)


def _assert_no_network(runner):
    for argv in runner.calls:
        joined = " ".join(argv)
        assert "fetch" not in joined and "ls-remote" not in joined and "pull" not in joined, joined


CHECK_COMMAND = (
    "out=$(grep -rn VIOLATION_MARKER . --include='*.txt' 2>/dev/null); "
    'if [ -n "$out" ]; then echo "$out" | sed "s/^/FAIL: /"; exit 1; fi; exit 0'
)

NOISY_COMMAND = 'echo "debug: pid=$$"; ' + CHECK_COMMAND

# Same check, but reporting ABSOLUTE paths — the shape that makes the `<VENUE>`
# substitution load-bearing (with the relative `.` above, delivery and base
# output are textually identical without any substitution at all).
ABS_CHECK_COMMAND = (
    'out=$(grep -rn VIOLATION_MARKER "$PWD" --include="*.txt" 2>/dev/null); '
    'if [ -n "$out" ]; then echo "$out" | sed "s/^/FAIL: /"; exit 1; fi; exit 0'
)

# `pwd -P` resolves symlinks, so this reports the venue's REALPATH rather than
# the (possibly symlinked) path the venue was addressed by.
PHYS_CHECK_COMMAND = (
    'out=$(grep -rn VIOLATION_MARKER "$(pwd -P)" --include="*.txt" 2>/dev/null); '
    'if [ -n "$out" ]; then echo "$out" | sed "s/^/FAIL: /"; exit 1; fi; exit 0'
)

SPEC = DifferentialSpec(target="main", remote="origin")


def _run_check(cwd, runner=None, command=CHECK_COMMAND):
    run = runner or subprocess_runner
    return run(["bash", "-c", f"cd {shlex.quote(str(cwd))} && {command}"])


# --- 1: non-degenerate resolution stamps -------------------------------------


def test_non_degenerate_resolution_stamps(tmp_path):
    work, origin = _init_repo(tmp_path)
    _commit(work, "extra.txt", "x\n", "T1 (local, not pushed)")
    trunk_head = _git(work, "rev-parse", "origin/main")

    state = _state()
    runner = _RecordingRunner()
    base_sha, refusal = differential.resolve_base(state, SPEC, str(work), runner)

    assert refusal is None
    assert base_sha == trunk_head
    assert state.differential_base["origin/main"] == trunk_head
    _assert_no_network(runner)


# --- 2: a rebase moves the stamp forward -------------------------------------


def test_rebase_moves_stamp_forward(tmp_path):
    work, origin = _init_repo(tmp_path)
    _commit(work, "extra.txt", "x\n", "T1 (local, not pushed)")
    t0 = _git(work, "rev-parse", "origin/main")

    state = _state()
    base_sha, refusal = differential.resolve_base(state, SPEC, str(work), None)
    assert refusal is None
    assert base_sha == t0

    advance = tmp_path / "advance"
    subprocess.run(["git", "clone", "-q", str(origin), str(advance)], check=True)
    _git(advance, "config", "user.email", "t@example.com")
    _git(advance, "config", "user.name", "T")
    _git(advance, "checkout", "-q", "-B", "main", "origin/main")
    _commit(advance, "trunk_advance.txt", "y\n", "trunk advance")
    _git(advance, "push", "-q", "origin", "main")

    _git(work, "fetch", "-q", "origin")
    _git(work, "rebase", "-q", "origin/main")
    t1 = _git(work, "rev-parse", "origin/main")
    assert t1 != t0

    base_sha_2, refusal_2 = differential.resolve_base(state, SPEC, str(work), None)
    assert refusal_2 is None
    assert base_sha_2 == t1
    assert base_sha_2 != base_sha
    assert state.differential_base["origin/main"] == t1


# --- 3: degenerate falls back to the frozen value ----------------------------


def test_degenerate_falls_back_to_frozen(tmp_path):
    work, origin = _init_repo(tmp_path)
    _commit(work, "extra.txt", "x\n", "T1 (local, not pushed)")
    t0 = _git(work, "rev-parse", "origin/main")

    state = _state()
    base_sha, refusal = differential.resolve_base(state, SPEC, str(work), None)
    assert refusal is None
    assert base_sha == t0

    # "land": push work's HEAD to origin/main, then fetch so trunk == HEAD.
    _git(work, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")
    assert _git(work, "rev-parse", "HEAD") == _git(work, "rev-parse", "origin/main")

    base_sha_2, refusal_2 = differential.resolve_base(state, SPEC, str(work), None)
    assert refusal_2 is None
    assert base_sha_2 == t0
    assert state.differential_base["origin/main"] == t0


# --- 4: degenerate with nothing frozen refuses -------------------------------


def test_degenerate_with_nothing_frozen_refuses(tmp_path):
    work, origin = _init_repo(tmp_path)
    state = _state()
    base_sha, refusal = differential.resolve_base(state, SPEC, str(work), None)
    assert base_sha is None
    assert refusal is not None
    assert "degenerate" in refusal
    assert "origin/main" not in state.differential_base


# --- 5: an unresolvable remote ref refuses -----------------------------------


def test_unresolvable_remote_ref_refuses(tmp_path):
    work, origin = _init_repo(tmp_path)
    spec = DifferentialSpec(target="no-such-branch", remote="origin")
    state = _state()
    base_sha, refusal = differential.resolve_base(state, spec, str(work), None)
    assert base_sha is None
    assert refusal is not None
    assert "origin/no-such-branch" in refusal


# --- 6: a worktree-add failure refuses and leaks no directory ---------------


def test_worktree_add_failure_refuses_and_leaks_no_directory(tmp_path, monkeypatch):
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(
        differential.tempfile,
        "mkdtemp",
        lambda prefix="": real_mkdtemp(prefix=prefix, dir=str(tmp_path)),
    )

    def runner(argv):
        if "worktree" in argv and "add" in argv:
            return RunResult(128, "", "fatal: boom")
        return RunResult(0, "", "")

    with differential.base_worktree("/repo", "deadbeef", runner) as (path, refusal):
        assert path is None
        assert refusal is not None
        assert "boom" in refusal

    assert list(tmp_path.iterdir()) == []


# --- 7: base 126/127 refuses -------------------------------------------------


@pytest.mark.parametrize("exit_code", [126, 127])
def test_base_unrunnable_exit_refuses(tmp_path, exit_code):
    work, origin = _init_repo(tmp_path)
    _commit(work, "extra.txt", "x\n", "T1")
    state = _state()
    delivery = RunResult(1, "FAIL: something", "")

    def runner(argv):
        if argv[:2] == ["git", "-C"] and "worktree" in argv:
            return RunResult(0, "", "")
        if argv[0] == "bash":
            return RunResult(exit_code, "", "command not found")
        return subprocess_runner(argv)

    verdict = differential.evaluate(state, SPEC, CHECK_COMMAND, 0, str(work), delivery, runner)
    assert verdict.status == "refused"
    assert str(exit_code) in verdict.refusal


# --- 8: base green => red ----------------------------------------------------


def test_base_green_delivery_red_is_red(tmp_path):
    work, origin = _init_repo(tmp_path)  # trunk is clean, no violation
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "introduce violation")
    state = _state()
    delivery = _run_check(work)
    assert delivery.returncode == 1

    verdict = differential.evaluate(state, SPEC, CHECK_COMMAND, 0, str(work), delivery, None)
    assert verdict.status == "red"
    assert verdict.new_violations
    assert "viol_a.txt" in " ".join(verdict.new_violations)


# --- 9: identical violations => green ---------------------------------------


def test_identical_violation_is_green(tmp_path):
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "trunk-side violation")
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "unrelated.txt", "hi\n", "unrelated local change")
    state = _state()
    delivery = _run_check(work)
    assert delivery.returncode == 1

    verdict = differential.evaluate(state, SPEC, CHECK_COMMAND, 0, str(work), delivery, None)
    assert verdict.status == "green"
    assert verdict.new_violations == []


# --- 10: one extra violation => red ------------------------------------------


def test_one_extra_violation_is_red(tmp_path):
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "trunk-side violation")
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "viol_b.txt", "VIOLATION_MARKER\n", "new violation")
    state = _state()
    delivery = _run_check(work)
    assert delivery.returncode == 1

    verdict = differential.evaluate(state, SPEC, CHECK_COMMAND, 0, str(work), delivery, None)
    assert verdict.status == "red"
    assert len(verdict.new_violations) == 1
    assert "viol_b.txt" in verdict.new_violations[0]
    assert "viol_a.txt" not in " ".join(verdict.new_violations)


# --- 11: a line-number shift in an unchanged violation is not counted as new


def test_line_number_shift_not_counted_as_new(tmp_path):
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "trunk-side violation")
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "viol_a.txt", "\nVIOLATION_MARKER\n", "shift the marker down one line")
    state = _state()
    delivery = _run_check(work)
    assert delivery.returncode == 1

    verdict = differential.evaluate(state, SPEC, CHECK_COMMAND, 0, str(work), delivery, None)
    assert verdict.status == "green"
    assert verdict.new_violations == []


# --- 12: violation_pattern filters out unrelated noise -----------------------


def test_violation_pattern_filters_noise(tmp_path):
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "trunk-side violation")
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "unrelated.txt", "hi\n", "unrelated local change")
    state = _state()
    spec = DifferentialSpec(target="main", remote="origin", violation_pattern=r"^FAIL:")

    # The raw text differs between delivery and base runs (each bash
    # subprocess's own $$), but the FAIL: lines — the actual violations —
    # are identical; only the pattern filter makes that visible.
    delivery = subprocess_runner(["bash", "-c", f"cd {shlex.quote(str(work))} && {NOISY_COMMAND}"])
    assert delivery.returncode == 1
    assert "debug: pid=" in delivery.stdout

    verdict = differential.evaluate(state, spec, NOISY_COMMAND, 0, str(work), delivery, None)
    assert verdict.status == "green"


# --- 13: A3' with a pattern — no base run issued -----------------------------


def test_empty_evidence_with_pattern_is_red_no_base_run(tmp_path):
    work, origin = _init_repo(tmp_path)
    state = _state()
    spec = DifferentialSpec(target="main", remote="origin", violation_pattern=r"^FAIL:")
    delivery = RunResult(1, "unrelated traceback, no FAIL: prefix", "")

    calls: list[list[str]] = []

    def runner(argv):
        calls.append(argv)
        return subprocess_runner(argv)

    verdict = differential.evaluate(state, spec, CHECK_COMMAND, 0, str(work), delivery, runner)
    assert verdict.status == "red"
    assert verdict.base_sha is None
    assert calls == []


# --- 14: A3' without a pattern — red against an explicitly red base ---------


def test_empty_evidence_without_pattern_is_red_even_against_red_base(tmp_path):
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "trunk-side violation, base is RED")
    state = _state()
    delivery = RunResult(1, "", "")

    calls: list[list[str]] = []

    def runner(argv):
        calls.append(argv)
        return subprocess_runner(argv)

    verdict = differential.evaluate(state, SPEC, CHECK_COMMAND, 0, str(work), delivery, runner)
    assert verdict.status == "red"
    assert verdict.base_sha is None
    assert calls == []


# --- 15: no-network argv assertion over a full happy-path run ---------------


def test_no_network_argv_across_full_evaluate(tmp_path):
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "trunk-side violation")
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "viol_b.txt", "VIOLATION_MARKER\n", "new violation")
    state = _state()
    runner = _RecordingRunner()
    delivery = _run_check(work, runner)
    assert delivery.returncode == 1

    verdict = differential.evaluate(state, SPEC, CHECK_COMMAND, 0, str(work), delivery, runner)
    assert verdict.status == "red"
    assert runner.calls
    _assert_no_network(runner)


# --- 16: a path-anchored pattern never reads a new violation as green -------


def test_path_anchored_pattern_does_not_hide_a_new_violation(tmp_path):
    """The delivery side must be normalized IDENTICALLY wherever it is
    consumed. With two normalizations — one with `paths`, one without — a
    pattern that matches raw path text passes the A3' guard and is then
    filtered out of the comparison, leaving an empty delivery multiset that
    subtracts to green while a genuinely-new violation is on screen."""
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "trunk-side violation")
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "viol_b.txt", "VIOLATION_MARKER\n", "genuinely new violation")
    state = _state()
    spec = DifferentialSpec(
        target="main", remote="origin", violation_pattern=re.escape(str(work))
    )
    delivery = _run_check(work, command=ABS_CHECK_COMMAND)
    assert delivery.returncode == 1
    assert "viol_b.txt" in delivery.stdout

    verdict = differential.evaluate(
        state, spec, ABS_CHECK_COMMAND, 0, str(work), delivery, None
    )
    # The pattern is incompatible with the venue substitution, so the honest
    # verdict is the A3' empty-evidence red that says so — never green.
    assert verdict.status == "red"
    assert verdict.base_sha is None
    assert "no comparable violation line" in verdict.message


# --- 17: a <VENUE>-anchored pattern still evaluates --------------------------


def test_venue_anchored_pattern_still_evaluates(tmp_path):
    """The mirror of case 16: a pattern written against the NORMALIZED line
    (which the plan's method invites) must not make every failing check
    spuriously A3'-red — it has to reach the base comparison."""
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "trunk-side violation")
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "viol_b.txt", "VIOLATION_MARKER\n", "genuinely new violation")
    state = _state()
    spec = DifferentialSpec(
        target="main", remote="origin", violation_pattern=r"^FAIL: <VENUE>/"
    )
    delivery = _run_check(work, command=ABS_CHECK_COMMAND)
    assert delivery.returncode == 1

    verdict = differential.evaluate(
        state, spec, ABS_CHECK_COMMAND, 0, str(work), delivery, None
    )
    assert verdict.status == "red"
    assert verdict.base_sha is not None  # it reached the base run, not A3'
    assert len(verdict.new_violations) == 1
    assert "viol_b.txt" in verdict.new_violations[0]
    assert verdict.new_violations[0].startswith("FAIL: <VENUE>/")


# --- 18: the comparison is a multiset, not a set -----------------------------


def test_repeated_normalized_line_is_a_multiset_difference(tmp_path):
    """Base has ONE instance of a violation line; delivery has TWO that
    normalize to the same `:L:` text. A multiset difference reports the second
    instance as new; a set difference cancels it and reads green."""
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "one trunk-side instance")
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\nVIOLATION_MARKER\n", "a second instance")
    state = _state()
    delivery = _run_check(work)
    assert delivery.returncode == 1

    verdict = differential.evaluate(state, SPEC, CHECK_COMMAND, 0, str(work), delivery, None)
    assert verdict.status == "red"
    assert verdict.new_violations == ["FAIL: ./viol_a.txt:L:VIOLATION_MARKER"]


# --- 19: <VENUE> substitution makes absolute paths comparable ----------------


def test_venue_substitution_makes_absolute_paths_comparable(tmp_path):
    """An unchanged violation reported by absolute path is green only because
    the venue and the base worktree both normalize to `<VENUE>`; without the
    substitution every line differs and the differential is falsely red."""
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "trunk-side violation")
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "unrelated.txt", "hi\n", "unrelated local change")
    state = _state()
    delivery = _run_check(work, command=ABS_CHECK_COMMAND)
    assert delivery.returncode == 1
    assert f"{work}/viol_a.txt" in delivery.stdout

    verdict = differential.evaluate(
        state, SPEC, ABS_CHECK_COMMAND, 0, str(work), delivery, None
    )
    assert verdict.status == "green"
    assert verdict.new_violations == []


# --- 20: the substitution covers the venue's realpath too --------------------


def test_venue_realpath_is_substituted(tmp_path):
    """A check that resolves symlinks reports the venue's realpath, which a
    literal substitution of the symlinked path the venue was addressed by
    would miss — turning every base line into a false new violation."""
    work, origin = _init_repo(tmp_path)
    _commit(work, "viol_a.txt", "VIOLATION_MARKER\n", "trunk-side violation")
    _git(work, "push", "-q", "origin", "main")
    _commit(work, "unrelated.txt", "hi\n", "unrelated local change")
    venue = tmp_path / "symlinked-venue"
    venue.symlink_to(work)
    state = _state()
    delivery = _run_check(venue, command=PHYS_CHECK_COMMAND)
    assert delivery.returncode == 1
    assert f"{work}/viol_a.txt" in delivery.stdout
    assert str(venue) not in delivery.stdout

    verdict = differential.evaluate(
        state, SPEC, PHYS_CHECK_COMMAND, 0, str(venue), delivery, None
    )
    assert verdict.status == "green"
    assert verdict.new_violations == []


# --- 21-23: the rest of the refusal taxonomy (A5) ----------------------------


def test_no_venue_refuses():
    state = _state()
    base_sha, refusal = differential.resolve_base(state, SPEC, None, None)
    assert base_sha is None
    assert "no check venue" in refusal


def test_rev_parse_head_failure_refuses():
    state = _state()

    def runner(argv):
        return RunResult(128, "", "fatal: not a git repository")

    base_sha, refusal = differential.resolve_base(state, SPEC, "/nowhere", runner)
    assert base_sha is None
    assert "rev-parse HEAD" in refusal
    assert "not a git repository" in refusal


@pytest.mark.parametrize("merge_base_stdout", ["", "noise on stdout\n"])
def test_merge_base_failure_refuses(merge_base_stdout):
    """Both disjuncts of the failure test are pinned: a merge-base that
    reports nothing, and one that exits nonzero while still writing stdout
    (a wrapper or alias in front of git) — the latter is caught by the
    returncode alone."""
    state = _state()

    def runner(argv):
        if "merge-base" in argv:
            return RunResult(
                128, merge_base_stdout, "fatal: refusing to work with unrelated histories"
            )
        return RunResult(0, "abc123\n", "")

    base_sha, refusal = differential.resolve_base(state, SPEC, "/nowhere", runner)
    assert base_sha is None
    assert "merge-base" in refusal
    assert "unrelated histories" in refusal
    assert "origin/main" not in state.differential_base
