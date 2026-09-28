"""Mutation-catalogue negative control for the read/write add_dir base
collision fix in `stage_grant_rules` (spawn-permission-grant-model stage 2).

Builds TEN known-bad subjects -- S0 (the module exactly as it stood just
before the fix, recovered from this checkout's own git history) and the
nine named wrong-fixes M1-M9 of the plan's mutation catalogue, each an
exact textual substitution applied to a scratch copy of the FIXED module
-- runs the gc1..gc11 cases from test_spawn_specialist_grants.py against
each, and requires every subject to turn RED exactly the cases the
catalogue lists beside it while leaving at least one case OUTSIDE that set
GREEN. The FIXED row runs the unmutated module and requires EVERY case
GREEN, so a regression in the fix itself cannot hide behind a mutant whose
required cases it happens to redden.

REDDENED has exactly one meaning: the case was collected exactly once and
FAILED in its call phase. A case collected zero or two-or-more times, or
one that errored/skipped instead of failing, is a control failure for
that requirement rather than proof the case discriminates -- so a subject
that fails to import (mutation dies, anchor rots) reddens nothing and
greens nothing, and is correctly read as broken rather than as vindicated.

Exit code is NATURAL: zero iff every subject satisfies its row of the
catalogue AND the union of every subject's listed cases is exactly
gc1..gc11 AND this script's own scratch work left scripts/ exactly as it
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
from typing import Callable, Optional

SCRIPT_PATH = Path(__file__).resolve()
SCRIPTS_DIR = SCRIPT_PATH.parent.parent
ROOT = SCRIPTS_DIR.parent
MODULE_RELPATH = "scripts/spawn-specialist.py"
FIXED_MODULE = ROOT / MODULE_RELPATH
TEST_FILE = SCRIPTS_DIR / "tests" / "test_spawn_specialist_grants.py"
FIX_MARKER = "write_bases: set[str] = set()"

ALL_CASES = {f"gc{n}" for n in range(1, 12)}

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
    "gc10": "test_gc10_nested_read_child_write_parent_refused",
    "gc11": "test_gc11_prefix_sibling_is_not_nested",
}


class AnchorError(RuntimeError):
    """An M1-M6 substitution's anchor was not found exactly once."""


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


def _s0_source() -> str:
    """The module at the parent of the oldest commit that introduced
    FIX_MARKER into it. A clone whose history does not reach that parent
    (shallow, or the marker never landed) fails the control rather than
    reviewing some other revision as S0."""
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

_PREPASS_BLOCK = '''    write_bases: set[str] = set()
    for entry in entries:
        if entry.get("rule") is not None:
            continue
        path = entry.get("path")
        mode = entry.get("mode")
        if path is None or mode is None:
            continue
        if mode == "write":
            write_bases.add(grants.rule_file_arg(path.rstrip("/")))

    allow: list[str] = []
    deny: list[str] = []
'''

_PREPASS_MODE_LINE = '''        if mode == "write":
            write_bases.add(grants.rule_file_arg(path.rstrip("/")))
'''

_READ_BLOCK = '''        if mode == "read":
            if base in write_bases:
                continue
            deny.append(f"Edit({base}/**)")
'''

_READ_WRITE_BLOCK = '''        if mode == "read":
            if base in write_bases:
                continue
            deny.append(f"Edit({base}/**)")
        elif mode == "write":
            allow.append(f"Edit({base}/**)")
            deny.extend(
'''

_WRITE_BLOCK_FULL = '''        elif mode == "write":
            allow.append(f"Edit({base}/**)")
            deny.extend(
                [
                    f"Edit({base}/**/.claude/**)",
                    f"Edit({base}/**/settings*.json)",
                    f"Edit({base}/**/.git/**)",
                    f"Edit({base}/**/.git)",
                ]
            )
'''

_WRITE_ALLOW_LINE = '''        elif mode == "write":
            allow.append(f"Edit({base}/**)")
'''


def _mutant_m1(source: str) -> str:
    """Never emit a read-derived deny at all."""
    return _substitute(source, _READ_BLOCK, '        if mode == "read":\n            pass\n', "M1")


def _mutant_m2(source: str) -> str:
    """Suppress read denies whenever ANY write entry is present anywhere."""
    anchor = '''        if mode == "read":
            if base in write_bases:
                continue
'''
    replacement = '''        if mode == "read":
            if write_bases:
                continue
'''
    return _substitute(source, anchor, replacement, "M2")


def _mutant_m3(source: str) -> str:
    """Decide each entry from the entries seen so far (no separate pre-pass)."""
    step1 = _substitute(
        source, _PREPASS_BLOCK,
        '    write_bases: set[str] = set()\n    allow: list[str] = []\n    deny: list[str] = []\n',
        "M3 (pre-pass removal)",
    )
    step2 = _substitute(
        step1, _WRITE_ALLOW_LINE,
        '        elif mode == "write":\n            write_bases.add(base)\n            allow.append(f"Edit({base}/**)")\n',
        "M3 (write-branch population)",
    )
    return step2


def _mutant_m4(source: str) -> str:
    """Resolve the collision by dropping the write ALLOW instead of the read DENY."""
    step1 = _substitute(
        source, _PREPASS_MODE_LINE,
        '        if mode == "read":\n            write_bases.add(grants.rule_file_arg(path.rstrip("/")))\n',
        "M4 (pre-pass mode flip)",
    )
    step2 = _substitute(
        step1, _READ_WRITE_BLOCK,
        '''        if mode == "read":
            deny.append(f"Edit({base}/**)")
        elif mode == "write":
            if base not in write_bases:
                allow.append(f"Edit({base}/**)")
            deny.extend(
''',
        "M4 (branch swap)",
    )
    return step2


def _mutant_m5(source: str) -> str:
    """Suppress EVERY deny whose base carries a write entry, guard denies included."""
    return _substitute(source, _WRITE_BLOCK_FULL, _WRITE_ALLOW_LINE, "M5")


def _mutant_m6(source: str) -> str:
    """The write-base pre-pass omits the rstrip('/') the emitting loop applies."""
    return _substitute(
        source, _PREPASS_MODE_LINE,
        '        if mode == "write":\n            write_bases.add(grants.rule_file_arg(path))\n',
        "M6",
    )


def _mutant_m7(source: str) -> str:
    """Same-base suppression matches by string prefix instead of exact base."""
    return _substitute(
        source,
        '            if base in write_bases:\n',
        '            if any(w.startswith(base) for w in write_bases):\n',
        "M7",
    )


def _mutant_m8(source: str) -> str:
    """The nesting check matches by string prefix instead of path segment."""
    return _substitute(
        source,
        '    return child_base.startswith(parent_base.rstrip("/") + "/")\n',
        '    return child_base != parent_base and child_base.startswith(parent_base)\n',
        "M8",
    )


def _mutant_m9(source: str) -> str:
    """The nesting check only catches a write under a read, not a read under a write."""
    return _substitute(
        source,
        '            if _is_strictly_under(other_base, base):\n',
        '            if mode == "read" and _is_strictly_under(other_base, base):\n',
        "M9",
    )


def _unmutated(source: str) -> str:
    return source


# name -> (builder or None for S0, required-red case set); an EMPTY set
# means every case must be GREEN, not merely one outside the set.
SUBJECTS: list[tuple[str, Optional[Callable[[str], str]], set[str]]] = [
    ("FIXED", _unmutated, set()),
    ("S0", None, {"gc1", "gc7", "gc9", "gc10"}),
    ("M1", _mutant_m1, {"gc4"}),
    ("M2", _mutant_m2, {"gc6"}),
    ("M3", _mutant_m3, {"gc1", "gc5", "gc7"}),
    ("M4", _mutant_m4, {"gc2", "gc7"}),
    ("M5", _mutant_m5, {"gc3"}),
    ("M6", _mutant_m6, {"gc8"}),
    ("M7", _mutant_m7, {"gc11"}),
    ("M8", _mutant_m8, {"gc11"}),
    ("M9", _mutant_m9, {"gc10"}),
]


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
        s0_source = _s0_source()

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

            missing_red = [c for c in sorted(required) if not _reddened(outcomes[c])]
            outside = ALL_CASES - required
            not_green = sorted(c for c in outside if not _greened(outcomes[c]))
            if required:
                outside_ok = len(not_green) < len(outside)
            else:
                outside_ok = not not_green

            subject_ok = not missing_red and outside_ok
            ok = ok and subject_ok

            detail = ", ".join(
                f"{c}={outcomes[c][1] or 'not-collected'}(x{outcomes[c][0]})" for c in sorted(required)
            )
            status_word = "OK" if subject_ok else "FAIL"
            extra = ""
            if missing_red:
                extra += f" missing_red={missing_red}"
            if not outside_ok:
                extra += f" not_green={not_green}" if not required else " no_green_outside_listed_set"
            lines.append(f"{name} [{status_word}]: required={sorted(required)} ({detail}){extra}")

        if union != ALL_CASES:
            ok = False
            lines.append(f"CATALOGUE [FAIL]: union of listed cases {sorted(union)} != {sorted(ALL_CASES)}")
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
