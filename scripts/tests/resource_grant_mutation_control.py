#!/usr/bin/env python3
"""Mutation controls for the resource-grants work: run a stage's real verify on a wrong copy.

Difficulty removed: a negative control that only asserts "this test file would fail" proves
nothing about the stage's full verify, and a hand-written control per stage drifts from it.
This tool applies one wrong version of one production line (an exact textual substitution)
to a scratch copy of the repository and then runs ANY command there, so the control is the
stage's own verify on a mutated tree and can pass only by a real kill.

  python3 scripts/tests/resource_grant_mutation_control.py --list
  python3 scripts/tests/resource_grant_mutation_control.py --control [--run CMD]
  python3 scripts/tests/resource_grant_mutation_control.py --mutant NAME [--run CMD]

The scratch copy is a `git clone --local --quiet` of this repository at HEAD with the
uncommitted changes (modified, staged, deleted AND untracked files) copied over, so it holds
what the working tree holds. Nothing is written to the source tree.

CMD is ONE argv command split with shlex and run without a shell: `&&`, `;`, pipes and `$VAR`
are not interpreted. Wrap a compound check in a script and pass the script.

Exit codes:
  --mutant NAME --run CMD   CMD is run twice: first on the unmutated copy, which must exit 0
                            (otherwise nothing is mutated and the tool exits 0, since a red
                            run on the mutant would prove nothing), then on the mutated copy,
                            whose exit code is the tool's. 1 means CMD failed (a kill for a
                            verify command); 0 means CMD passed on the wrong code (survived).
                            pytest's 2/4/5 (collection error, usage, nothing collected) pass
                            through unchanged, so they never read as a kill.
                            EXCEPT: exit 0 when the anchor does not match exactly once, the
                            copy fails, the unmutated run is not green, or this tool itself
                            raises -- a broken control must never read as a kill (a crash
                            under --control is 7, never green).
  --mutant NAME             0 when the mutant applies; no command is run.
  --control [--run CMD]     no mutation. Without --run: 0 when every anchor of the catalogue
                            applies exactly once in the copy, 7 otherwise. With --run: CMD's
                            exit code on the unmutated copy (green is 0).
  --list                    0.

While CMD runs, RESOURCE_GRANT_ACTIVE_MUTANT=<name> is exported; the catalogue test, whose
anchor tests would fail inside a mutated copy, skips those tests when it sees it.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_MUTANT_ENV = "RESOURCE_GRANT_ACTIVE_MUTANT"
CONTROL_RED = 7

PLAN_RESOURCES = "scripts/agentctl/plan_resources.py"
NORM_HANDOFF = "scripts/agentctl/norm_handoff.py"
PLAN = "scripts/agentctl/plan.py"
SPAWN = "scripts/spawn-specialist.py"
GRANTS = "scripts/agentctl/grants.py"
CLI = "scripts/agentctl/cli.py"


@dataclass(frozen=True)
class Mutant:
    relpath: str
    anchor: str
    replacement: str
    description: str


CATALOGUE: "dict[str, Mutant]" = {
    "handoff-resources-carried": Mutant(
        NORM_HANDOFF,
        '"resources": list(resources or []),',
        '"resources": [],',
        "the handoff record drops the stage's typed resources",
    ),
    "workdir-write-baseline": Mutant(
        SPAWN,
        'baseline = agentctl_resources.FileResource(workdir, "write")',
        'baseline = agentctl_resources.FileResource(workdir, "read")',
        "the acceptEdits workdir baseline is read, not write",
    ),
    "workdir-baseline-needs-accept-edits": Mutant(
        SPAWN,
        'return permission_mode == "acceptEdits" and not write_grant_forces_accept_edits(kind, engine_grants)',
        "return not write_grant_forces_accept_edits(kind, engine_grants)",
        "the workdir baseline no longer depends on the resolved permission mode",
    ),
    "wildcard-kept-as-pattern": Mutant(
        PLAN_RESOURCES,
        'elif res.reason_class == "wildcard-tail":\n                out.rule_patterns.append(entry["rule"])',
        'elif res.reason_class == "wildcard-tail":\n                pass',
        "a wildcard-tail Bash rule is lost instead of kept as a rule pattern",
    ),
    "digest-check-detects-change": Mutant(
        PLAN,
        ") + order_place(doc.meta) + optional_stages_place(doc))",
        ") + order_place(doc.meta) + optional_stages_place(doc) + ('mutant',))",
        "the plan content digest moves; digest_neutrality_check.py must exit 1",
    ),
    "digest-check-detects-premise-key-change": Mutant(
        PLAN,
        "payload = repr((element, value))",
        "payload = repr((element, value, 'mutant'))",
        "only a plan-level premise key moves; digest_neutrality_check.py must exit 1",
    ),
    "push-own-branch-only": Mutant(
        GRANTS,
        'resources_.append(_resources.VcsRefResource(BRANCH_REMOTE, branch, "push"))',
        'resources_.append(_resources.VcsRefResource(BRANCH_REMOTE, "main", "push"))',
        "the derived push names trunk instead of the plan-declared branch",
    ),
    "push-trunk-refused": Mutant(
        GRANTS,
        '    if (folded in _TRUNK_BRANCHES or folded.startswith(("release-", "release/"))\n'
        "            or (head is not None and folded == head.casefold())):",
        "    if False:",
        "trunk, release-* and the remote HEAD branch are no longer refused",
    ),
    "ref-form-trunk-refused": Mutant(
        GRANTS,
        "    if not _is_plain_branch_name(branch, venue):",
        "    if False:",
        "HEAD and ref-path spellings of trunk (refs/heads/main, origin/main) derive a push",
    ),
    "branch-frozen-at-approve": Mutant(
        CLI,
        "    if stored is None:\n"
        "        return None\n"
        "    if entries_grants_sha256(stored) != state.approved_grants_sha256:\n"
        "        return []\n"
        '    frozen = (stored.get(str(stage_index)) or {}).get("branch")',
        "    if True:\n"
        "        return None\n"
        '    frozen = (stored.get(str(stage_index)) or {}).get("branch")',
        "spawn and stage-grants re-derive DR-P from the repository as it is now",
    ),
    "dropped-surfaced": Mutant(
        CLI,
        '    if dropped:\n        d.data["dropped_grants"] = dropped',
        '    if False:\n        d.data["dropped_grants"] = dropped',
        "submit-plan no longer reports the dropped grants",
    ),
    "branch-change-is-grant-growth": Mutant(
        PLAN,
        "        if set(new_branch[stage.index]) - set(old_branch.get(stage.index, [])):\n"
        "            return True",
        "        if False:\n"
        "            return True",
        "a replan that moves the stage's branch counts as a self-approvable refinement",
    ),
    "branch-resources-hashed": Mutant(
        PLAN,
        '    payload += _branch_hash_part({\n'
        '        idx: list((s.get("branch") or {}).get("resources") or []) for idx, s in entries.items()})\n',
        "",
        "the frozen DR-P resources fall out of the stored entries' hash",
    ),
    "refined-branch-limited-to-admitted": Mutant(
        PLAN,
        '        "resources": [d for d in now["branch"]["resources"] if tuple(sorted(d.items())) in admitted],',
        '        "resources": list(now["branch"]["resources"]),',
        "a refinement hands the child whatever the new plan derives, admitted or not",
    ),
    "refined-branch-frozen-only": Mutant(
        PLAN,
        '    admitted = {tuple(sorted(d.items())) for d in frozen.get("resources") or []}\n    return {',
        '    admitted = {tuple(sorted(d.items())) for d in frozen.get("resources") or []}\n'
        '    admitted |= {tuple(sorted(d.items())) for d in now["branch"]["resources"]}\n    return {',
        "a refinement re-admits what a live read of origin/HEAD or the refs now derives",
    ),
}


class ScratchError(Exception):
    """The scratch copy could not be made."""


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if proc.returncode != 0:
        raise ScratchError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _paths(raw: bytes) -> "list[str]":
    return [p for p in raw.decode("utf-8", errors="surrogateescape").split("\0") if p]


def make_scratch(repo: Path, dest: Path) -> Path:
    """Clone `repo` at HEAD into `dest/repo` and copy its uncommitted state over:
    modified and staged files, deletions, and untracked (not ignored) files."""
    clone = dest / "repo"
    proc = subprocess.run(
        ["git", "clone", "--local", "--quiet", str(repo), str(clone)], capture_output=True, check=False)
    if proc.returncode != 0:
        raise ScratchError(f"git clone failed: {proc.stderr.decode(errors='replace').strip()}")
    changed = _paths(_git(repo, "diff", "HEAD", "--name-only", "--no-renames", "-z"))
    untracked = _paths(_git(repo, "ls-files", "--others", "--exclude-standard", "-z"))
    for rel in dict.fromkeys(changed + untracked):
        src, dst = repo / rel, clone / rel
        if src.is_symlink() or src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            shutil.copy2(src, dst, follow_symlinks=False)
        elif dst.is_symlink() or dst.exists():
            dst.unlink()
    return clone


def _anchor_count(root: Path, mutant: Mutant) -> int:
    """How many times the anchor occurs in the mutant's file under `root` (0 if unreadable)."""
    try:
        return (root / mutant.relpath).read_text(encoding="utf-8").count(mutant.anchor)
    except OSError:
        return 0


def apply_mutant(root: Path, mutant: Mutant) -> bool:
    """Substitute the anchor in `root`; False (nothing written) unless it occurs exactly once."""
    if _anchor_count(root, mutant) != 1:
        return False
    target = root / mutant.relpath
    target.write_text(target.read_text(encoding="utf-8").replace(mutant.anchor, mutant.replacement),
                      encoding="utf-8")
    return True


def anchors_apply_once(root: Path, catalogue: "dict[str, Mutant]") -> "list[str]":
    """The names of catalogue entries whose anchor does not occur exactly once under `root`."""
    return [name for name, mutant in catalogue.items() if _anchor_count(root, mutant) != 1]


def run_command(cmd: str, cwd: Path, env_extra: "dict[str, str] | None" = None) -> int:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env_extra or {})}
    code = subprocess.run(shlex.split(cmd), cwd=cwd, env=env, check=False).returncode
    return 128 - code if code < 0 else code


def run_mutant(
    name: str, run: "str | None" = None, *, repo: Path = REPO_ROOT,
    catalogue: "dict[str, Mutant] | None" = None,
) -> int:
    mutant = (CATALOGUE if catalogue is None else catalogue).get(name)
    if mutant is None:
        print(f"resource-grant-mutation: no mutant named {name!r}", file=sys.stderr)
        return 0
    with tempfile.TemporaryDirectory(prefix="resource-grant-mutation-") as tmp:
        try:
            clone = make_scratch(repo, Path(tmp))
        except ScratchError as exc:
            print(f"resource-grant-mutation: scratch copy failed, no mutation applied: {exc}", file=sys.stderr)
            return 0
        if _anchor_count(clone, mutant) != 1:
            print(f"resource-grant-mutation: anchor of {name} does not match exactly once in "
                  f"{mutant.relpath}; nothing was mutated", file=sys.stderr)
            return 0
        if run is not None:
            baseline = run_command(run, clone)
            if baseline != 0:
                print(f"resource-grant-mutation: the command already exits {baseline} on the unmutated "
                      f"copy, so a red run on the mutant would prove nothing; no mutation applied",
                      file=sys.stderr)
                return 0
        if not apply_mutant(clone, mutant):
            print(f"resource-grant-mutation: {name} did not apply; nothing was mutated", file=sys.stderr)
            return 0
        print(f"resource-grant-mutation: applied {name} ({mutant.description})")
        if run is None:
            return 0
        return run_command(run, clone, {ACTIVE_MUTANT_ENV: name})


def run_control(
    run: "str | None" = None, *, repo: Path = REPO_ROOT, catalogue: "dict[str, Mutant] | None" = None,
) -> int:
    catalogue = CATALOGUE if catalogue is None else catalogue
    with tempfile.TemporaryDirectory(prefix="resource-grant-mutation-") as tmp:
        try:
            clone = make_scratch(repo, Path(tmp))
        except ScratchError as exc:
            print(f"resource-grant-mutation: scratch copy failed: {exc}", file=sys.stderr)
            return CONTROL_RED
        missing = anchors_apply_once(clone, catalogue)
        if missing:
            print(f"resource-grant-mutation: anchor does not occur exactly once for: {', '.join(missing)}",
                  file=sys.stderr)
            return CONTROL_RED
        if run is None:
            print(f"resource-grant-mutation: control green -- {len(catalogue)} anchor(s) each occur exactly once")
            return 0
        return run_command(run, clone)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true")
    mode.add_argument("--control", action="store_true")
    mode.add_argument("--mutant", metavar="NAME")
    ap.add_argument("--run", metavar="CMD", help="one argv command (shlex-split, no shell) to run in the scratch copy")
    args = ap.parse_args(argv)
    try:
        if args.list:
            for name, mutant in CATALOGUE.items():
                print(f"{name}\t{mutant.relpath}\t{mutant.description}")
            return 0
        if args.control:
            return run_control(args.run)
        return run_mutant(args.mutant, args.run)
    except Exception:
        traceback.print_exc()
        if args.mutant is not None:
            print("resource-grant-mutation: internal error; exiting 0 so it cannot read as a kill", file=sys.stderr)
            return 0
        print("resource-grant-mutation: internal error; the control is not green", file=sys.stderr)
        return CONTROL_RED


if __name__ == "__main__":
    sys.exit(main())
