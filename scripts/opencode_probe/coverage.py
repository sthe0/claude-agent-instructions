"""Which changed files are subjects of a probe phase.

A file changed between two revisions that is neither the suite itself (scripts/tests/,
scripts/opencode_probe/, the CLI shim, Markdown) nor listed in a phase's ``SUBJECT_FILES`` or in
``SUBJECT_ONLY`` is uncovered: a change to it would not stale any recorded pass.
"""
from __future__ import annotations

import subprocess

from . import core, subject_only

SUITE_PREFIXES = ("scripts/tests/", "scripts/opencode_probe/")
SUITE_FILES = ("scripts/opencode-probe.py",)


def changed_files(since: str, until: str) -> list[str]:
    result = subprocess.run(["git", "-C", str(core.REPO_ROOT), "diff", "--name-only", f"{since}..{until}"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise core.InternalError(f"git diff {since}..{until} failed: {result.stderr.strip()[-200:]}")
    return [line for line in result.stdout.splitlines() if line]


def uncovered(since: str, until: str) -> list[str]:
    subjects = set(subject_only.SUBJECT_ONLY)
    for phase in core.discover_phases():
        subjects.update(phase.subject_files)
    out = []
    for rel in changed_files(since, until):
        if rel.startswith(SUITE_PREFIXES) or rel in SUITE_FILES or rel.endswith(".md"):
            continue
        if rel not in subjects:
            out.append(rel)
    return out
