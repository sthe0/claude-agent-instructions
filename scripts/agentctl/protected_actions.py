"""Catalogue of protected actions: actions a standing norm lets nobody take without the
user's explicit confirmation, which a broad grant would otherwise authorize silently.

A grant is a decision the plan author wrote down and the engine derived; whether a rule
covers a protected action is decidable by probing exact commands with the matcher the
harness-side cover uses (`grants.bash_command_covered_by_rules`). So the engine can show
every covering rule at presentation and hold `approve` until the user accepts it
(`gates.protected_grant_blockers`, `accept-protected-grant`), and the child-contour
boundary (`gates.parent_norm_boundary`) can refuse to let a wildcard stand in for a
protected check. Pure: reads only its arguments and, for the freshness check, the repo.

Each entry's `quote` must occur verbatim after an anchor comment naming the entry's id in
its `norm_ref` file (`anchor_marker`), so a norm reworded without the catalogue is caught
by `norm_anchor_problems`, and so is an anchor with no entry.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import grants as _grants

REPO_ROOT = Path(__file__).resolve().parents[2]

_ANCHOR_PREFIX = "<!-- protected-action: "
_ANCHOR_SUFFIX = " -->"
ANCHOR_RE = re.compile(re.escape(_ANCHOR_PREFIX) + r"([a-z0-9-]+)" + re.escape(_ANCHOR_SUFFIX))

_SCANNED_SUFFIXES = frozenset({".md", ".toml", ".py", ".txt"})
_SKIPPED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", "node_modules", ".claude"})


@dataclass(frozen=True)
class ProtectedAction:
    id: str
    probe_commands: tuple[str, ...]
    norm_ref: str
    quote: str


CATALOGUE: tuple[ProtectedAction, ...] = (
    ProtectedAction(
        id="push-trunk",
        probe_commands=("git push origin main", "git push origin release-1"),
        norm_ref="memory-global/leaves/landing-discipline.md",
        quote="Trunk/main-push needs explicit confirmation",
    ),
)


def anchor_marker(entry_id: str) -> str:
    return f"{_ANCHOR_PREFIX}{entry_id}{_ANCHOR_SUFFIX}"


def entry_by_id(entry_id: str) -> ProtectedAction | None:
    return next((e for e in CATALOGUE if e.id == entry_id), None)


def wildcard_hits(rules) -> list[tuple[str, str]]:
    """The `(rule, entry id)` pairs whose rule covers a probe command of the entry —
    the exact test the harness-side cover applies, so a rule hit here would let the
    harness run the protected action without asking. A narrow rule such as
    `Bash(git push origin feature:*)` covers no probe and produces no hit."""
    hits: list[tuple[str, str]] = []
    for rule in dict.fromkeys(rules):
        for entry in CATALOGUE:
            if any(_grants.bash_command_covered_by_rules([rule], probe)
                   for probe in entry.probe_commands):
                hits.append((rule, entry.id))
    return hits


def protected_ids_of_command(command: str) -> list[str]:
    """The entry ids whose probe `command` is, or extends with further arguments. Used
    to refuse a wildcard as the cover of a delegated check that performs a protected
    action: such a check is covered only by an identical entry the parent holds."""
    text = (command or "").strip()
    return [e.id for e in CATALOGUE
            if any(text == probe or text.startswith(probe + " ") for probe in e.probe_commands)]


def norm_anchor_problems(repo_root: Path | None = None) -> list[str]:
    """Freshness of the catalogue against the norms it quotes, both directions: every
    entry's quote occurs after its anchor in its `norm_ref`, and every anchor in the
    repo names a catalogue entry. [] == fresh."""
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    problems: list[str] = []
    for entry in CATALOGUE:
        path = root / entry.norm_ref
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{entry.id}: norm_ref {entry.norm_ref} is unreadable ({exc})")
            continue
        marker = anchor_marker(entry.id)
        at = text.find(marker)
        if at < 0:
            problems.append(f"{entry.id}: {entry.norm_ref} has no {marker} anchor")
        elif entry.quote not in text[at + len(marker):]:
            problems.append(f"{entry.id}: the quote {entry.quote!r} does not occur after the anchor "
                            f"in {entry.norm_ref}")
    known = {e.id for e in CATALOGUE}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIPPED_DIRS]
        for name in filenames:
            if Path(name).suffix not in _SCANNED_SUFFIXES:
                continue
            path = Path(dirpath) / name
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for found in ANCHOR_RE.findall(text):
                if found not in known:
                    problems.append(f"{path.relative_to(root)} anchors protected action {found!r}, "
                                    "which has no catalogue entry")
    return problems
