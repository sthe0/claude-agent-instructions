"""Differential-verify: re-run a FAILED check at the delivery branch's
merge-base against a trunk and report green iff no NEW violation appears
relative to that base (schema 25 — see state.py's `DifferentialSpec`
docstring and the differential-verify plan's design axes A1-A6). This module
implements axes A2/A3/A3'/A5; A1 (the typed spec) and A6 (opt-in wiring) are
Stage 1, already landed.

Base resolution (axis A2): `merge-base(HEAD, <remote>/<target>)`, re-resolved
on every call and re-stamped into `state.differential_base` whenever the
resolution is NON-degenerate (HEAD not yet contained in the trunk). Once the
branch has landed, the live resolution degenerates (merge-base == HEAD); at
that point the frozen stamp is used instead, and resolution refuses if
nothing was ever frozen. This is what lets the base follow a rebase while the
branch is un-landed and still be usable after it lands.

Violation comparison (axis A3): the comparison unit is the check's own
output, normalized and compared as a MULTISET — never the exit code — so a
base that is already red does not mask a genuinely new failure. Axis A3': an
EMPTY normalized+filtered delivery violation set on a FAILED check is never
green (the empty set is a sub-multiset of every set); it resolves to RED,
decided BEFORE the base is resolved, so a crash producing no comparable
output never costs a base run.

Refusal (axis A5): an unanswerable base (an unresolvable ref, a failed
merge-base, a degenerate resolution with nothing frozen, a worktree that
cannot be created, or a base run exiting 126/127) is a THIRD verdict value,
distinct from both green and red — mirrors `_resolve_or_refuse` in cli.py.

The throwaway-worktree and leaking-git-env-scrub shapes below are RE-DERIVED
from `scripts/lib/baseline_diff.py` (branch `si/baseline-diff-granularity`,
unmerged as of this writing) rather than imported: that module solves a
different problem (HEAD-vs-working-tree, keyed on a structured Python
`finder`), and its API does not fit an opaque shell command evaluated at an
arbitrary base commit whose only observable is text. See design axis A4 in
the plan.

This module issues no network git command (no `fetch`, no `pull`, no
`ls-remote`) — only `rev-parse`, `merge-base`, and `worktree add`/`remove`,
all local. It imports only from `.dispatch` and `.state`; nothing here
depends on `cli.py`."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .dispatch import Runner, RunResult, subprocess_runner
from .state import DifferentialSpec, SessionState

# The git location/index env vars a hook subprocess inherits; a nested `git
# worktree add` silently misbehaves (can reset the CALLER's index) if these
# leak through. agentctl is itself invoked from git hooks, so the scrub
# applies here too. Re-derived from baseline_diff.py's `_LEAKING_GIT_ENV`.
_LEAKING_GIT_ENV = (
    "GIT_INDEX_FILE",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_NAMESPACE",
    "GIT_INDEX_VERSION",
)

_POSITION_RE = re.compile(r":\d+:")
_BASE_UNRUNNABLE_EXITS = (126, 127)


@contextmanager
def _clean_git_env() -> Iterator[None]:
    saved = {k: os.environ.pop(k) for k in _LEAKING_GIT_ENV if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


@dataclass
class DifferentialVerdict:
    """The three-valued result of `evaluate()`.

    `status` is one of "green" / "red" / "refused" (axis A5 — refusal is a
    distinct member, never folded into red or green). `base_sha` is set
    whenever a base was actually resolved (the green and red cases that
    reached a base run; None for a refusal or an axis-A3' empty-evidence red,
    neither of which ever resolves one). `new_violations` carries the
    normalized lines a red verdict should report — always in the same
    `<VENUE>`-substituted, `:L:`-collapsed form, whichever red branch
    produced them, since an operator reads this field directly. `refusal`
    is the unanswerable-base message for a refused verdict (None otherwise).
    `message` is always a human-readable summary."""

    status: str
    base_sha: str | None = None
    new_violations: list[str] = field(default_factory=list)
    refusal: str | None = None
    message: str = ""


def normalize(text: str, *, paths: tuple[str | None, ...] = ()) -> list[str]:
    """The comparison unit for a differential: split `text` on newlines,
    rstrip each line, drop blank lines, replace every occurrence of each
    path in `paths` with the literal `<VENUE>` (so the delivery venue and the
    base worktree — two different directories — normalize to the same line),
    and collapse `:<digits>:` position tokens to `:L:` (so an unrelated
    line-number shift of an otherwise-unchanged violation is not read as
    new — the same stable-identity property external baseline tools get from
    a content hash).

    Each path is substituted BOTH as given and as its realpath: a check that
    resolves symlinks (`realpath`, `pwd -P`, a Go/Rust tool that canonicalizes)
    emits the resolved form, which a literal substitution of a symlinked venue
    path — a symlinked `TMPDIR` is the common case — would miss, making every
    base line differ and turning the whole differential falsely red.
    Substitution runs longest-first (then lexicographically, so the order is
    reproducible): a symlink and its target are routinely in a prefix
    relation, and substituting the shorter one first would leave a mangled
    tail on the longer. Empty and None entries are dropped."""
    targets: set[str] = set()
    for p in paths:
        if not p:
            continue
        targets.add(p)
        targets.add(os.path.realpath(p))
    ordered = sorted(targets, key=lambda p: (-len(p), p))
    out = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        for p in ordered:
            line = line.replace(p, "<VENUE>")
        line = _POSITION_RE.sub(":L:", line)
        out.append(line)
    return out


def _filter_pattern(lines: list[str], pattern: str | None) -> list[str]:
    """Keep only lines matching `pattern`, applied AFTER normalization; None
    keeps every line."""
    if pattern is None:
        return lines
    rx = re.compile(pattern)
    return [line for line in lines if rx.search(line)]


def _combined_output(result: RunResult) -> str:
    """stdout+stderr combined: the comparison is a multiset, so interleaving
    order between the two streams is irrelevant."""
    return (result.stdout or "") + "\n" + (result.stderr or "")


def resolve_base(
    state: SessionState,
    spec: DifferentialSpec,
    venue_cwd: str | None,
    runner: Runner | None,
) -> tuple[str | None, str | None]:
    """Resolve the differential's base per axis A2: `merge-base HEAD
    <remote>/<target>` at `venue_cwd`. Re-stamps
    `state.differential_base["<remote>/<target>"]` whenever the resolution is
    NON-degenerate (HEAD not yet contained in the trunk) — the one call site
    that writes that map. A degenerate resolution (the post-land case) falls
    back to the frozen stamp; with nothing ever frozen, or any of the three
    git commands failing, this refuses rather than guessing.

    Returns `(base_sha, refusal)`; exactly one is None."""
    run = runner or subprocess_runner
    key = f"{spec.remote}/{spec.target}"
    if not venue_cwd:
        return None, "differential: no check venue resolved to run 'git rev-parse HEAD' in"

    head_result = run(["git", "-C", venue_cwd, "rev-parse", "HEAD"])
    head = head_result.stdout.strip()
    if head_result.returncode != 0 or not head:
        return None, (
            f"differential: 'git rev-parse HEAD' failed in {venue_cwd!r}: "
            f"{head_result.stderr.strip()}"
        )

    trunk_result = run(["git", "-C", venue_cwd, "rev-parse", "--verify", key])
    trunk = trunk_result.stdout.strip()
    if trunk_result.returncode != 0 or not trunk:
        return None, (
            f"differential: {key!r} does not resolve in {venue_cwd!r} (no "
            "fetch will be attempted; fetch the ref yourself if it is stale)"
        )

    merge_base_result = run(["git", "-C", venue_cwd, "merge-base", head, trunk])
    base = merge_base_result.stdout.strip()
    if merge_base_result.returncode != 0 or not base:
        return None, (
            f"differential: 'git merge-base {head} {trunk}' failed in "
            f"{venue_cwd!r}: {merge_base_result.stderr.strip()}"
        )

    if base != head:
        state.differential_base[key] = base
        return base, None

    frozen = state.differential_base.get(key)
    if frozen:
        return frozen, None
    return None, (
        f"differential: base resolution for {key!r} is degenerate (HEAD is "
        "already contained in the trunk) and no base was ever frozen for it; "
        "the stage that rebases onto the trunk must record its result while "
        "the branch is still un-landed so a base gets stamped before it lands"
    )


@contextmanager
def base_worktree(
    repo_cwd: str, commitish: str, runner: Runner | None
) -> Iterator[tuple[str | None, str | None]]:
    """A throwaway detached worktree at `commitish`, added from `repo_cwd`.

    Yields `(path, refusal)`; exactly one is None. Cleanup runs on every exit
    path — `git worktree remove --force` first (only if the add succeeded),
    then an unconditional `shutil.rmtree`, so a partially-added worktree
    never leaks a directory. Wrapped in the leaking-git-env scrub (see module
    docstring)."""
    run = runner or subprocess_runner
    with _clean_git_env():
        tmpdir = tempfile.mkdtemp(prefix="agentctl-differential-base-")
        added = False
        try:
            result = run(
                ["git", "-C", repo_cwd, "worktree", "add", "--detach", "--quiet", tmpdir, commitish]
            )
            if result.returncode != 0:
                yield None, (
                    f"differential: could not create a base worktree at "
                    f"{commitish!r}: {result.stderr.strip()}"
                )
                return
            added = True
            yield tmpdir, None
        finally:
            if added:
                run(["git", "-C", repo_cwd, "worktree", "remove", "--force", tmpdir])
            shutil.rmtree(tmpdir, ignore_errors=True)


def evaluate(
    state: SessionState,
    spec: DifferentialSpec,
    command: str,
    expected_exit: int,
    venue_cwd: str | None,
    delivery_result: RunResult,
    runner: Runner | None,
) -> DifferentialVerdict:
    """The public entry point: given a check that has ALREADY FAILED (its
    `delivery_result` — the caller ran it; this never re-runs the delivery
    side), decide green / red / refused per axes A2/A3/A3'/A5. Raises nothing
    of its own: every failure mode it decides is a typed refusal on the
    returned verdict. It does not shield the caller from the injected
    `Runner`, though — the default `subprocess_runner` does not guard
    `subprocess.run`, so an absent `git` binary still propagates."""
    # The delivery side is normalized EXACTLY ONCE, and both the A3' guard
    # below and the comparison further down consume this one list. Two
    # separate normalizations with different `paths` would disagree whenever
    # `violation_pattern` matches against path text: a genuinely-new violation
    # could pass the guard and then be filtered out of the comparison, leaving
    # an empty delivery multiset that subtracts to green. `base_dir` is
    # deliberately absent from `paths` — the throwaway worktree cannot appear
    # in output produced before it existed.
    delivery_evidence = _filter_pattern(
        normalize(_combined_output(delivery_result), paths=(venue_cwd,)),
        spec.violation_pattern,
    )
    if not delivery_evidence:
        # Axis A3': empty evidence is never green, checked FIRST — before any
        # base resolution or base run — so a crash producing no comparable
        # violation line costs nothing beyond the delivery run already made.
        return DifferentialVerdict(
            status="red",
            message=(
                "the check failed but produced no comparable violation line "
                "(empty after normalization and the `violation_pattern` "
                "filter); the differential subtracted nothing — fix the "
                "failure, or widen `violation_pattern` so it is expressible "
                "as a violation"
            ),
        )

    base_sha, refusal = resolve_base(state, spec, venue_cwd, runner)
    if refusal:
        return DifferentialVerdict(status="refused", refusal=refusal, message=refusal)

    run = runner or subprocess_runner
    with base_worktree(venue_cwd, base_sha, runner) as (base_dir, wt_refusal):
        if wt_refusal:
            return DifferentialVerdict(
                status="refused", base_sha=base_sha, refusal=wt_refusal, message=wt_refusal
            )

        base_result = run(["bash", "-c", f"cd {shlex.quote(base_dir)} && {command}"])

        if base_result.returncode in _BASE_UNRUNNABLE_EXITS:
            msg = (
                f"differential: the check could not run at base {base_sha} "
                f"(exit {base_result.returncode})"
            )
            return DifferentialVerdict(status="refused", base_sha=base_sha, refusal=msg, message=msg)

        if base_result.returncode == expected_exit:
            # The base is green: nothing to subtract, every delivery
            # violation is new.
            return DifferentialVerdict(
                status="red",
                base_sha=base_sha,
                new_violations=delivery_evidence,
                message=f"the base ({base_sha}) passed; every violation is new",
            )

        base_lines = _filter_pattern(
            normalize(_combined_output(base_result), paths=(venue_cwd, base_dir)),
            spec.violation_pattern,
        )
        new = list((Counter(delivery_evidence) - Counter(base_lines)).elements())
        if not new:
            return DifferentialVerdict(
                status="green",
                base_sha=base_sha,
                message=f"identical to base {base_sha}: no new violation",
            )
        return DifferentialVerdict(
            status="red",
            base_sha=base_sha,
            new_violations=new,
            message=f"{len(new)} new violation(s) relative to base {base_sha}",
        )
