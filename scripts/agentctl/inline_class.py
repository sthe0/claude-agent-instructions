"""The inline ("small change") task class: its definition, and the size it is measured by.

Difficulty removed: the class exists so a change with no choice and no risk is made in-thread
without a plan gate, but it was defined by proxies (a file count, a raw changed-line count) that
generated files and tests dominate, so a one-line fix could not be inline while a large
generated-registry refresh could not be told from a design change. This module states the
criteria once, and measures the one proxy that is kept as a backstop (hand-written production
lines) over exactly the lines a reviewer would weigh.

Pure: no file, process or network I/O. The consumers that run git (scripts/inline-measure.py, the
calibrator, the mandate driver) build their argv with `numstat_argv` / `check_attr_argv` and
parse the output with `parse_numstat_z` / `generated_from_check_attr`, so each rule has one copy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .exempt_paths import is_production_file

BOUND_KEY = "small-change-max-lines"

INLINE_DEFINITION = (
    "A change is inline (a small change) only when there is no choice and no risk in it: "
    "one obvious solution, with no design decision; the change is reversible; "
    "it has no external or irreversible effect, no public-contract change and no new dependency; "
    "it is one cohesive change (a module and its tests); "
    "and it is at most small-change-max-lines hand-written production lines "
    "(tests, generated files and memory leaves are not counted)."
)

CRITERIA = (
    "one obvious solution",
    "reversible",
    "irreversible effect",
    "public-contract change",
    "new dependency",
    "one cohesive change",
    BOUND_KEY,
    "hand-written production lines",
)

TEST_PATTERNS = ("scripts/tests/**", "**/tests/**", "test_*.py", "*_test.py")
MEMORY_PATTERNS = ("memory-global/leaves/**",)

GENERATED_ATTR = "linguist-generated"
_GENERATED_VALUES = frozenset({"set", "true"})


def _token_re(token: str) -> "re.Pattern[str]":
    return re.compile(r"(?<!\w)" + re.escape(token) + r"(?!\w)")


def missing_criteria(text: str) -> "tuple[str, ...]":
    """The CRITERIA tokens `text` lacks, each matched on word boundaries so that
    "reversible" is not satisfied by "irreversible effect"."""
    return tuple(token for token in CRITERIA if not _token_re(token).search(text))


def small_change_reason(bound: int) -> str:
    return f"small change: {INLINE_DEFINITION} [{BOUND_KEY}={bound}]"


def size_signal(lines: int, bound: int) -> str:
    return f"{lines} hand-written production lines (> {BOUND_KEY}={bound})"


@dataclass(frozen=True)
class Change:
    path: str
    added: int = 0
    deleted: int = 0
    old_path: "str | None" = None
    binary: bool = False

    @property
    def paths(self) -> "tuple[str, ...]":
        return (self.old_path, self.path) if self.old_path else (self.path,)


@dataclass(frozen=True)
class Measure:
    production_lines: int = 0
    counted_paths: "tuple[str, ...]" = ()
    excluded: "dict[str, tuple[str, ...]]" = field(default_factory=dict)
    binary_paths: "tuple[str, ...]" = ()


def numstat_argv(base: str, tip: str) -> "list[str]":
    return ["git", "diff", "--numstat", "-z", "-M", base, tip]


def check_attr_argv(source: "str | None" = None) -> "list[str]":
    """`source` reads the attributes from that commit's tree instead of the working tree (git >= 2.40)."""
    pinned = [] if source is None else [f"--source={source}"]
    return ["git", "check-attr", *pinned, "-z", "--stdin", GENERATED_ATTR]


def check_attr_stdin(changes: "list[Change]") -> str:
    """NUL-terminated path list for `check_attr_argv()`: both sides of every rename."""
    return "".join(p + "\0" for ch in changes for p in ch.paths)


def parse_numstat_z(output: str) -> "list[Change]":
    """Parse `git diff --numstat -z -M`: `added\\tdeleted\\tpath\\0`, or for a rename
    `added\\tdeleted\\t\\0old\\0new\\0`; a binary file shows `-\\t-`."""
    tokens = output.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    changes: "list[Change]" = []
    i = 0
    while i < len(tokens):
        added, deleted, path = tokens[i].split("\t", 2)
        i += 1
        old_path = None
        if path == "":
            old_path, path = tokens[i], tokens[i + 1]
            i += 2
        binary = added == "-" and deleted == "-"
        changes.append(Change(
            path=path,
            added=0 if binary else int(added),
            deleted=0 if binary else int(deleted),
            old_path=old_path,
            binary=binary,
        ))
    return changes


def generated_from_check_attr(z_output: str) -> "frozenset[str]":
    """Paths marked generated in `git check-attr -z --stdin linguist-generated` output
    (`path\\0attr\\0value\\0` triples); only the value `set` or `true` marks one."""
    tokens = z_output.split("\0")
    generated = set()
    for i in range(0, len(tokens) - 2, 3):
        path, attr, value = tokens[i:i + 3]
        if attr == GENERATED_ATTR and value in _GENERATED_VALUES:
            generated.add(path)
    return frozenset(generated)


def _glob_re(pattern: str) -> "re.Pattern[str]":
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _matches_any(path: str, patterns: "tuple[str, ...]") -> bool:
    for pattern in patterns:
        if "/" in pattern:
            if _glob_re(pattern).match(path):
                return True
        elif _glob_re(pattern).match(path.rsplit("/", 1)[-1]):
            return True
    return False


def is_counted(path: str, generated: "frozenset[str]") -> bool:
    return (
        is_production_file(path)
        and not _matches_any(path, TEST_PATTERNS)
        and not _matches_any(path, MEMORY_PATTERNS)
        and path not in generated
    )


def _exclusion_reason(path: str, generated: "frozenset[str]") -> str:
    if path in generated:
        return "generated"
    if _matches_any(path, TEST_PATTERNS):
        return "test"
    if _matches_any(path, MEMORY_PATTERNS):
        return "memory"
    return "non-production"


def measure(changes: "list[Change]", generated: "frozenset[str]") -> Measure:
    """A rename counts if either side counts; lines are added + deleted."""
    lines = 0
    counted: "list[str]" = []
    binary: "list[str]" = []
    excluded: "dict[str, list[str]]" = {}
    for ch in changes:
        if any(is_counted(p, generated) for p in ch.paths):
            counted.append(ch.path)
            if ch.binary:
                binary.append(ch.path)
            lines += ch.added + ch.deleted
        else:
            excluded.setdefault(_exclusion_reason(ch.path, generated), []).append(ch.path)
    return Measure(
        production_lines=lines,
        counted_paths=tuple(counted),
        excluded={reason: tuple(paths) for reason, paths in excluded.items()},
        binary_paths=tuple(binary),
    )


def lines_within_bound(lines: int, bound: int) -> bool:
    return lines <= bound


def within_bound(measured: Measure, bound: int) -> bool:
    if measured.binary_paths:
        return False
    return lines_within_bound(measured.production_lines, bound)
