"""Mutation-catalogue negative control for the read/write add_dir coverage
rule in `stage_grant_rules` (spawn-permission-grant-model stage 2).

Builds SEVENTEEN known-bad subjects -- S0 (the module exactly as it stood
just before the first collision fix, recovered from this checkout's own git
history) and the sixteen named wrong-fixes M1-M16 of the mutation
catalogue, each an exact textual substitution applied to a scratch copy of
the FIXED module -- runs the gc1..gc19 cases from
test_spawn_specialist_grants.py against each, and requires every subject to
turn RED every case the catalogue lists beside it while leaving at least one
case OUTSIDE that set GREEN. The FIXED row runs the unmutated module and
requires EVERY case GREEN, so a regression in the fix itself cannot hide
behind a mutant whose required cases it happens to redden. Each row also
prints the full set of cases the subject actually reddened, so a listed set
that has drifted from the observed one is visible.

REDDENED has exactly one meaning: the case was collected exactly once and
FAILED in its call phase. A case collected zero or two-or-more times, or
one that errored/skipped instead of failing, is a control failure for
that requirement rather than proof the case discriminates -- so a subject
that fails to import (mutation dies, anchor rots) reddens nothing and
greens nothing, and is correctly read as broken rather than as vindicated.

Exit code is NATURAL: zero iff every subject satisfies its row of the
catalogue AND the union of every subject's listed cases is exactly
gc1..gc19 AND this script's own scratch work left scripts/ exactly as it
found it. The plan's negative_control field supplies the `!` inversion;
this script's own exit code is never inverted here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable, Optional

SCRIPT_PATH = Path(__file__).resolve()
SCRIPTS_DIR = SCRIPT_PATH.parent.parent
ROOT = SCRIPTS_DIR.parent
MODULE_RELPATH = "scripts/spawn-specialist.py"
FIXED_MODULE = ROOT / MODULE_RELPATH
TEST_FILE = SCRIPTS_DIR / "tests" / "test_spawn_specialist_grants.py"
FIX_MARKER = "Claude client resolves a rule present in both lists as DENY"

ALL_CASES = {f"gc{n}" for n in range(1, 20)}

CASE_FUNCS = {
    "gc1": "test_gc1_no_string_in_both_lists",
    "gc2": "test_gc2_write_allow_survives",
    "gc3": "test_gc3_write_guard_denies_survive",
    "gc4": "test_gc4_read_only_base_still_denies",
    "gc5": "test_gc5_order_independence",
    "gc6": "test_gc6_per_base_scope",
    "gc7": "test_gc7_real_producer_via_derive_stage_grants",
    "gc8": "test_gc8_trailing_slash_normalization",
    "gc9": "test_gc9_nested_read_parent_write_child_refused",
    "gc10": "test_gc10_read_child_is_covered_by_write_parent",
    "gc11": "test_gc11_prefix_sibling_is_not_nested",
    "gc12": "test_gc12_dot_segment_spelling_refused_like_plain",
    "gc13": "test_gc13_double_slash_spelling_refused_like_plain",
    "gc14": "test_gc14_dotdot_segment_refused_as_invalid",
    "gc15": "test_gc15_derived_read_under_runtime_write_is_covered",
    "gc16": "test_gc16_covered_read_is_no_conflict_with_a_write_under_it",
    "gc17": "test_gc17_nested_reads_each_keep_their_deny",
    "gc18": "test_gc18_nested_writes_each_keep_their_allow_and_guards",
    "gc19": "test_gc19_refusal_names_both_provenances",
}


class AnchorError(RuntimeError):
    """A mutant's substitution anchor was not found exactly once."""


def _substitute(source: str, anchor: str, replacement: str, label: str) -> str:
    count = source.count(anchor)
    if count != 1:
        raise AnchorError(f"{label}: anchor found {count} time(s), expected exactly 1")
    return source.replace(anchor, replacement, 1)


def _git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}")
    return result.stdout


def _s0_source(fixed_source: str) -> str:
    """The module at the parent of the oldest commit that introduced
    FIX_MARKER into it. FIX_MARKER must be a literal the fixed module
    carries and that parent lacks: a fixed module that has lost it, or a
    clone whose history does not reach that parent (shallow, or the marker
    never landed), fails the control rather than reviewing some other
    revision as S0."""
    if FIX_MARKER not in fixed_source:
        raise RuntimeError(f"{MODULE_RELPATH} no longer contains {FIX_MARKER!r}; re-point FIX_MARKER")
    introducing = _git("log", "--reverse", "--format=%H", "-S", FIX_MARKER, "--", MODULE_RELPATH).split()
    if not introducing:
        raise RuntimeError(f"no commit in this checkout's history introduces {FIX_MARKER!r} into {MODULE_RELPATH}")
    source = _git("show", f"{introducing[0]}^:{MODULE_RELPATH}")
    if FIX_MARKER in source:
        raise RuntimeError(f"S0 candidate {introducing[0]}^ already contains {FIX_MARKER!r}")
    return source


def _git_status() -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all", "--", ":(top)scripts"],
        capture_output=True, text=True,
    )
    return result.stdout


# --- mutation catalogue: each is an exact textual substitution on the -----
# --- FIXED module's own source, anchored to the literal current text.  ---

_READ_BRANCH = '''        elif _read_add_dir_needs_deny(item, add_dirs):
'''

_READ_DENY_BRANCH = _READ_BRANCH + '''            deny.append(f"Edit({item.base}/**)")
'''

_WRITE_ALLOW_LINE = '''            allow.append(f"Edit({item.base}/**)")
'''

_WRITE_GUARD_DENIES = '''            deny.extend(
                [
                    f"Edit({item.base}/**/.claude/**)",
                    f"Edit({item.base}/**/settings*.json)",
                    f"Edit({item.base}/**/.git/**)",
                    f"Edit({item.base}/**/.git)",
                ]
            )
'''

_COVERAGE_TEST = '''    if any(read.base == w.base or _is_strictly_under(read.base, w.base) for w in writes):
'''

_CANONICAL_RETURN = '''    return grants.rule_file_arg(str(normalized))
'''

_REFUSAL_TAIL = '''                f"the read's Edit deny would shadow part of the write's Edit allow; refused"
            )
    return True
'''


def _mutant_m1(source: str) -> str:
    """Never emit a read-derived deny at all."""
    return _substitute(source, _READ_DENY_BRANCH, _READ_BRANCH + "            pass\n", "M1")


def _mutant_m2(source: str) -> str:
    """Treat every read as covered whenever ANY write entry is present."""
    return _substitute(source, _COVERAGE_TEST, "    if writes:\n", "M2")


def _mutant_m3(source: str) -> str:
    """Decide each read from the add_dirs seen before it, not the whole list."""
    return _substitute(
        source, _READ_BRANCH,
        "        elif _read_add_dir_needs_deny(item, add_dirs[: add_dirs.index(item)]):\n",
        "M3",
    )


def _mutant_m4(source: str) -> str:
    """Resolve a same-base collision by dropping the write ALLOW instead of the read DENY."""
    step1 = _substitute(
        source, _WRITE_ALLOW_LINE,
        '            if not any(d.mode == "read" and d.base == item.base for d in add_dirs):\n'
        '                allow.append(f"Edit({item.base}/**)")\n',
        "M4 (write allow withheld)",
    )
    return _substitute(step1, _READ_BRANCH, "        else:\n", "M4 (read always denies)")


def _mutant_m5(source: str) -> str:
    """Emit a write's ALLOW without its four guard denies."""
    return _substitute(source, _WRITE_GUARD_DENIES, "", "M5")


def _mutant_m6(source: str) -> str:
    """Compare bases in their raw spelling, uncanonicalized."""
    return _substitute(source, _CANONICAL_RETURN, "    return grants.rule_file_arg(path)\n", "M6")


def _mutant_m7(source: str) -> str:
    """Coverage matches by string prefix instead of path segment."""
    return _substitute(
        source, _COVERAGE_TEST, "    if any(read.base.startswith(w.base) for w in writes):\n", "M7"
    )


def _mutant_m8(source: str) -> str:
    """Containment matches by string prefix instead of path segment."""
    return _substitute(
        source,
        '    return child_base.startswith(parent_base.rstrip("/") + "/")\n',
        '    return child_base != parent_base and child_base.startswith(parent_base)\n',
        "M8",
    )


def _mutant_m9(source: str) -> str:
    """Coverage matches only an identical base, not a read under the write."""
    return _substitute(
        source, _COVERAGE_TEST, "    if any(read.base == w.base for w in writes):\n", "M9"
    )


def _mutant_m10(source: str) -> str:
    """Canonicalize a base with a '..' segment instead of refusing it."""
    return _substitute(source, '    if ".." in normalized.parts:\n', "    if False:\n", "M10")


def _mutant_m11(source: str) -> str:
    """Keep coverage but drop the refusal of an uncovered read over a write."""
    return _substitute(
        source, "        if _is_strictly_under(write.base, read.base):\n", "        if False:\n", "M11"
    )


def _mutant_m12(source: str) -> str:
    """The containment refusal checks every add_dir, not only writes."""
    return _substitute(source, "    for write in writes:\n", "    for write in add_dirs:\n", "M12")


def _mutant_m13(source: str) -> str:
    """Check the containment refusal before coverage instead of after it."""
    step1 = _substitute(source, _COVERAGE_TEST + "        return False\n", "", "M13 (coverage removed)")
    return _substitute(
        step1, _REFUSAL_TAIL,
        _REFUSAL_TAIL.replace(
            "    return True\n",
            "    return not any(read.base == w.base or _is_strictly_under(read.base, w.base) for w in writes)\n",
        ),
        "M13 (coverage after refusal)",
    )


def _mutant_m14(source: str) -> str:
    """Normalize only a trailing slash, as the pre-coverage code did."""
    return _substitute(source, _CANONICAL_RETURN, '    return grants.rule_file_arg(path.rstrip("/"))\n', "M14")


def _mutant_m15(source: str) -> str:
    """Name the two paths in the refusal but not their provenance."""
    return _substitute(
        source,
        '                f"(provenance {write.provenance}) is nested under read add_dir "\n'
        '                f"{grants.rule_file_path(read.base)!r} (provenance {read.provenance}) — "\n',
        '                f"is nested under read add_dir "\n'
        '                f"{grants.rule_file_path(read.base)!r} — "\n',
        "M15",
    )


def _mutant_m16(source: str) -> str:
    """Refuse a write nested in another write as if it were a conflict."""
    return _substitute(
        source,
        '        elif item.mode == "write":\n',
        '        elif item.mode == "write":\n'
        '            if any(d.mode == "write" and _is_strictly_under(item.base, d.base) for d in add_dirs):\n'
        '                raise GrantShadowError(f"write add_dir {item.base} is nested under another write")\n',
        "M16",
    )


def _unmutated(source: str) -> str:
    return source


# name -> (builder or None for S0, required-red case set); an EMPTY set
# means every case must be GREEN, not merely one outside the set.
SUBJECTS: list[tuple[str, Optional[Callable[[str], str]], set[str]]] = [
    ("FIXED", _unmutated, set()),
    ("S0", None, {"gc1", "gc7", "gc8", "gc9", "gc10", "gc12", "gc13", "gc14", "gc15", "gc16", "gc19"}),
    ("M1", _mutant_m1, {"gc4", "gc6", "gc11", "gc17"}),
    ("M2", _mutant_m2, {"gc6", "gc9", "gc11", "gc12", "gc13", "gc19"}),
    ("M3", _mutant_m3, {"gc1", "gc5", "gc7", "gc8", "gc9", "gc10", "gc12", "gc13", "gc15", "gc19"}),
    ("M4", _mutant_m4, {"gc2", "gc7", "gc8", "gc9", "gc10", "gc12", "gc13", "gc15", "gc16", "gc19"}),
    ("M5", _mutant_m5, {"gc3", "gc18"}),
    ("M6", _mutant_m6, {"gc8", "gc12", "gc13"}),
    ("M7", _mutant_m7, {"gc11"}),
    ("M8", _mutant_m8, {"gc11"}),
    ("M9", _mutant_m9, {"gc10", "gc15"}),
    ("M10", _mutant_m10, {"gc14"}),
    ("M11", _mutant_m11, {"gc9", "gc12", "gc13", "gc19"}),
    ("M12", _mutant_m12, {"gc17"}),
    ("M13", _mutant_m13, {"gc16"}),
    ("M14", _mutant_m14, {"gc12", "gc13"}),
    ("M15", _mutant_m15, {"gc19"}),
    ("M16", _mutant_m16, {"gc16", "gc18"}),
]


def _ordered(cases: Iterable[str]) -> list[str]:
    return sorted(cases, key=lambda case_id: int(case_id[len("gc"):]))


def _run_subject(name: str, module_source: str, scratch_root: Path) -> dict[str, tuple[int, Optional[str]]]:
    """Runs `-k test_gc` against `module_source` and returns, per case id,
    (collection count, outcome) where outcome is "passed"/"failed"/"error"/
    "skipped"/None (never collected)."""
    subject_dir = scratch_root / name
    tests_dir = subject_dir / "tests"
    tests_dir.mkdir(parents=True)
    (subject_dir / "spawn-specialist.py").write_text(module_source)
    shutil.copyfile(TEST_FILE, tests_dir / "test_spawn_specialist_grants.py")

    report_path = subject_dir / "report.xml"
    env = dict(os.environ)
    path_parts = [str(SCRIPTS_DIR), str(SCRIPTS_DIR / "tests")]
    existing = env.get("PYTHONPATH")
    if existing:
        path_parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(path_parts)

    subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "--tb=no", "-k", "test_gc",
            f"--junitxml={report_path}", str(tests_dir / "test_spawn_specialist_grants.py"),
        ],
        cwd=str(subject_dir), env=env, capture_output=True, text=True,
    )

    counts: dict[str, int] = {}
    status: dict[str, str] = {}
    if report_path.exists():
        tree = ET.parse(report_path)
        for testcase in tree.getroot().iter("testcase"):
            fname = testcase.get("name", "")
            for case_id, func_name in CASE_FUNCS.items():
                if fname == func_name:
                    counts[case_id] = counts.get(case_id, 0) + 1
                    if testcase.find("failure") is not None:
                        status[case_id] = "failed"
                    elif testcase.find("error") is not None:
                        status[case_id] = "error"
                    elif testcase.find("skipped") is not None:
                        status[case_id] = "skipped"
                    else:
                        status[case_id] = "passed"

    return {cid: (counts.get(cid, 0), status.get(cid)) for cid in ALL_CASES}


def _reddened(outcome: tuple[int, Optional[str]]) -> bool:
    count, verdict = outcome
    return count == 1 and verdict == "failed"


def _greened(outcome: tuple[int, Optional[str]]) -> bool:
    count, verdict = outcome
    return count == 1 and verdict == "passed"


def main() -> int:
    before = _git_status()
    ok = True
    lines: list[str] = []
    union: set[str] = set()

    scratch_root = Path(tempfile.mkdtemp(prefix="gc-mutation-control-"))
    try:
        fixed_source = FIXED_MODULE.read_text()
        s0_source = _s0_source(fixed_source)

        for name, builder, required in SUBJECTS:
            union |= required
            if builder is None:
                source = s0_source
            else:
                try:
                    source = builder(fixed_source)
                except AnchorError as exc:
                    ok = False
                    lines.append(f"{name} [FAIL]: {exc}")
                    continue

            outcomes = _run_subject(name, source, scratch_root)

            missing_red = [c for c in _ordered(required) if not _reddened(outcomes[c])]
            outside = ALL_CASES - required
            not_green = _ordered(c for c in outside if not _greened(outcomes[c]))
            if required:
                outside_ok = len(not_green) < len(outside)
            else:
                outside_ok = not not_green

            subject_ok = not missing_red and outside_ok
            ok = ok and subject_ok

            detail = ", ".join(
                f"{c}={outcomes[c][1] or 'not-collected'}(x{outcomes[c][0]})" for c in _ordered(required)
            )
            reddened = _ordered(c for c in ALL_CASES if _reddened(outcomes[c]))
            status_word = "OK" if subject_ok else "FAIL"
            extra = ""
            if missing_red:
                extra += f" missing_red={missing_red}"
            if not outside_ok:
                extra += f" not_green={not_green}" if not required else " no_green_outside_listed_set"
            lines.append(f"{name} [{status_word}]: required={_ordered(required)} ({detail}) red={reddened}{extra}")

        if union != ALL_CASES:
            ok = False
            lines.append(f"CATALOGUE [FAIL]: union of listed cases {_ordered(union)} != {_ordered(ALL_CASES)}")
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)

    after = _git_status()
    if before != after:
        ok = False
        lines.append("WRITE-SURFACE [FAIL]: scripts/ porcelain changed during the run")

    for line in lines:
        print(line)

    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # any accident must exit non-zero, never zero
        print(f"CONTROL FAILURE: unhandled exception: {exc}", file=sys.stderr)
        sys.exit(1)
