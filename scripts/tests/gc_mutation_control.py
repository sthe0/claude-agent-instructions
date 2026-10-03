"""Mutation-catalogue negative control for the grant-materialization rules
in spawn-specialist.py and for the sweep that runs their tests.

It covers three case families:
  gc1..gc19  the read/write add_dir coverage rule in `stage_grant_rules`
             (test_spawn_specialist_grants.py);
  gs1..gs15  the single home of the four guard globs and the final check
             refusing an Edit allow an Edit deny covers entirely
             (test_spawn_specialist_grants.py; gs13..gs15 carry no ordinal
             in their function names, so cases are selected by exact
             CASE_FUNCS name, never by a `test_gs` substring);
  gw1..gw8   scripts/tests/sweep_grant_surface.sh -- its file list, its
             green-summary rule and what its `main` runs
             (test_sweep_grant_surface.py).

Builds FORTY-THREE known-bad subjects and runs all forty-two cases against
each one:
  S0     the module just before the first collision fix;
  S0_GS  the module just before the guard helper and the final check (both
         recovered from this checkout's own git history);
  S0_GW  a tree with no sweep script at all;
  and the forty named wrong-fixes M1-M17, G1-G15 and W1-W8. Each wrong
  fix is an exact textual substitution applied to a scratch copy of the
  FIXED module or the FIXED sweep script.
Every subject must turn RED every case the catalogue lists beside it while
leaving at least one case OUTSIDE that set GREEN. The forty-fourth subject,
FIXED, runs the unmutated tree and requires all forty-two cases GREEN, so
a regression in the fix itself cannot hide behind a mutant whose required
cases it happens to redden. Each row also prints the full set of cases the
subject actually reddened, so a listed set that has drifted from the
observed one is visible.

REDDENED has exactly one meaning: the case was collected exactly once and
FAILED in its call phase. A case collected zero or two-or-more times, or
one that errored/skipped instead of failing, is a control failure for
that requirement rather than proof the case discriminates -- so a subject
that fails to import (mutation dies, anchor rots) reddens nothing and
greens nothing, and is correctly read as broken rather than as vindicated.

Exit code is NATURAL. It is zero iff all three of these hold:
  - every subject satisfies its row of the catalogue;
  - within each family, the union of every subject's listed cases is the
    whole family;
  - this script's scratch work, all under one temp root, left scripts/
    exactly as it found it.
The plan's negative_control field supplies the `!` inversion; this
script's own exit code is never inverted here.

Wall-clock: one subject's pytest run measured at ~4.0 s on 2026-09-30, so
the forty-four subjects take ~2.9 min.
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
TESTS_DIR = SCRIPTS_DIR / "tests"
ROOT = SCRIPTS_DIR.parent
MODULE_RELPATH = "scripts/spawn-specialist.py"
SWEEP_RELPATH = "scripts/tests/sweep_grant_surface.sh"
FIXED_MODULE = ROOT / MODULE_RELPATH
FIXED_SWEEP = ROOT / SWEEP_RELPATH
CASE_FILES = ("test_spawn_specialist_grants.py", "test_sweep_grant_surface.py")
FIX_MARKER = "Claude client resolves a rule present in both lists as DENY"
GUARD_FIX_MARKER = "The guard globs are spelled in this one function only."

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
    "gs1": "test_gs1_guard_globs_have_one_home",
    "gs2": "test_gs2_file_allow_under_a_claude_guard_is_refused",
    "gs3": "test_gs3_write_add_dir_is_not_voided_by_its_own_guards",
    "gs4": "test_gs4_subtree_allow_beside_a_non_subtree_deny_survives",
    "gs5": "test_gs5_exact_path_allow_matched_by_a_non_subtree_deny_is_refused",
    "gs6": "test_gs6_subtree_allow_is_not_covered_by_a_non_subtree_deny",
    "gs7": "test_gs7_single_star_stays_inside_one_segment",
    "gs8": "test_gs8_subtree_deny_does_not_reach_a_prefix_sibling",
    "gs9": "test_gs9_stage_write_grant_under_the_repo_claude_guard_is_refused",
    "gs10": "test_gs10_refusal_quotes_both_rules_and_their_sources",
    "gs11": "test_gs11_file_allow_under_a_derived_read_names_both_sources",
    "gs12": "test_gs12_planner_plans_dir_inside_repo_claude_dir_gets_no_guards",
    "gs13": "test_shadow_refusal_names_each_side_producer_when_the_rule_texts_match",
    "gs14": "test_project_settings_own_shadowed_pair_warns_and_proceeds",
    "gs15": "test_project_settings_deny_over_an_engine_allow_still_refuses",
    "gw1": "test_gw1_every_sweep_file_exists",
    "gw2": "test_gw2_sweep_files_nonempty_without_duplicates",
    "gw3": "test_gw3_sweep_files_follow_the_membership_law",
    "gw4": "test_gw4_green_summary_shapes_are_accepted",
    "gw5": "test_gw5_non_green_summary_shapes_are_rejected",
    "gw6": "test_gw6_sourcing_does_not_run_main",
    "gw7": "test_gw7_required_defs_name_the_mutation_control_test",
    "gw8": "test_gw8_main_runs_one_unfiltered_pytest_and_judges_its_last_line",
}

FAMILIES = ("gc", "gs", "gw")
ALL_CASES = set(CASE_FUNCS)


def _family(case_id: str) -> str:
    return case_id[:2]


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


def _pre_marker_source(fixed_source: str, marker: str) -> str:
    """The module at the parent of the oldest commit that introduced
    `marker` into it. `marker` must be a literal the fixed module carries
    and that parent lacks: a fixed module that has lost it, or a clone whose
    history does not reach that parent (shallow, or the marker never
    landed), fails the control rather than reviewing some other revision as
    the pre-fix subject."""
    if marker not in fixed_source:
        raise RuntimeError(f"{MODULE_RELPATH} no longer contains {marker!r}; re-point the marker")
    introducing = _git("log", "--reverse", "--format=%H", "-S", marker, "--", MODULE_RELPATH).split()
    if not introducing:
        raise RuntimeError(f"no commit in this checkout's history introduces {marker!r} into {MODULE_RELPATH}")
    source = _git("show", f"{introducing[0]}^:{MODULE_RELPATH}")
    if marker in source:
        raise RuntimeError(f"pre-fix candidate {introducing[0]}^ already contains {marker!r}")
    return source


def _git_status() -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all", "--", ":(top)scripts"],
        capture_output=True, text=True,
    )
    return result.stdout


# --- mutation catalogue: each is an exact textual substitution on the -----
# --- FIXED module's or sweep script's own source, anchored to the     -----
# --- literal current text.                                             -----

_READ_BRANCH = '''        elif _read_add_dir_needs_deny(item, add_dirs):
'''

_READ_DENY_BRANCH = _READ_BRANCH + '''            deny.append(f"Edit({item.base}/**)")
'''

_WRITE_ALLOW_LINE = '''            allow.append(f"Edit({item.base}/**)")
'''

_WRITE_GUARD_DENIES_CALL = '''            deny.extend(write_guard_deny_rules(item.base, _guard_exempt_rel_paths(item.base, exempt_abs_paths or ())))
'''

_COVERAGE_TEST = '''    if any(read.base == w.base or _is_strictly_under(read.base, w.base) for w in writes):
'''

_CANONICAL_RETURN = '''    return grants.rule_file_arg(str(normalized))
'''

_REFUSAL_TAIL = '''                f"the read's Edit deny would shadow part of the write's Edit allow; refused"
            )
    return True
'''

_REPO_ROOT_GUARDS_RETURN = '''    base = grants.rule_file_arg(root.rstrip("/"))
    return write_guard_deny_rules(base, _guard_exempt_rel_paths(base, exempt_abs_paths))
'''

_FINAL_CHECK_CALL = '''    _check_no_allow_fully_denied(allow, deny, allow_source, deny_source)
'''

_REPO_ROOT_DENY_EXTEND = '''    if workdir is not None:
        add_deny(repo_root_deny_rules(kind, workdir, guard_exempt_paths or ()), "repo_root_deny_rules")
'''

_COVERAGE_BY_SHAPE = '''    if allow_path.endswith("/**"):
        allow_path = allow_path[: -len("/**")]
        if not deny_path.endswith("/**"):
            return False
    if "*" in allow_path:
        return False
    pattern = _edit_glob_regex(deny_path)
    return pattern is not None and pattern.fullmatch(allow_path) is not None
'''

_NON_SUBTREE_DENY_EXIT = '''        if not deny_path.endswith("/**"):
            return False
'''

_REFUSAL_SOURCES = '''                f"(source {allow_from}) "
                f"is entirely covered by Edit deny {deny_rule!r} "
                f"(source {deny_from}) "
'''

_DENY_SOURCE_MAP = '''    deny_source: dict[str, str] = {}
'''

_PROJECT_PAIR_TEST = '''            if allow_from == PROJECT_SETTINGS_SOURCE and deny_from == PROJECT_SETTINGS_SOURCE:
'''

_PROJECT_PAIR_WARNING = '''                print(
                    f"spawn-specialist: warning: Edit allow {allow_rule!r} is entirely covered by "
                    f"Edit deny {deny_rule!r}, so the allow is void — both come from the target "
                    f"project's own settings.local.json, so the spawn proceeds",
                    file=sys.stderr,
                )
'''

_REPO_ROOT_KIND_GATE = '''    of whether root strictly exceeds cwd."""
    if kind != "developer":
        return []
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
    return _substitute(source, _WRITE_GUARD_DENIES_CALL, "", "M5")


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


def _mutant_m17(source: str) -> str:
    """The containment refusal sees only the writes listed after the read."""
    return _substitute(
        source, "    for write in writes:\n",
        '    for write in [d for d in add_dirs[add_dirs.index(read) + 1:] if d.mode == "write"]:\n',
        "M17",
    )


def _mutant_g1(source: str) -> str:
    """Spell the four guard globs again inside repo_root_deny_rules."""
    return _substitute(
        source, _REPO_ROOT_GUARDS_RETURN,
        '    base = grants.rule_file_arg(root.rstrip("/"))\n'
        "    return [\n"
        '        f"Edit({base}/**/.claude/**)",\n'
        '        f"Edit({base}/**/settings*.json)",\n'
        '        f"Edit({base}/**/.git/**)",\n'
        '        f"Edit({base}/**/.git)",\n'
        "    ]\n",
        "G1",
    )


def _mutant_g2(source: str) -> str:
    """Never run the final coverage check."""
    return _substitute(source, _FINAL_CHECK_CALL, "", "G2")


def _mutant_g3(source: str) -> str:
    """Refuse any allow the deny's literal prefix merely intersects."""
    return _substitute(
        source, _COVERAGE_BY_SHAPE,
        '    if allow_path.endswith("/**"):\n'
        '        allow_path = allow_path[: -len("/**")]\n'
        '    literal = deny_path.split("*", 1)[0].rstrip("/")\n'
        '    return allow_path == literal or allow_path.startswith(literal + "/") or literal.startswith(allow_path + "/")\n',
        "G3",
    )


def _mutant_g4(source: str) -> str:
    """Run the final check before the repo-root guard denies are added."""
    return _substitute(
        source, _REPO_ROOT_DENY_EXTEND + _FINAL_CHECK_CALL, _FINAL_CHECK_CALL + _REPO_ROOT_DENY_EXTEND, "G4"
    )


def _mutant_g5(source: str) -> str:
    """Read `**` as exactly one segment."""
    return _substitute(source, 'parts.append("(?:/[^/]+)*")', 'parts.append("/[^/]+")', "G5")


def _mutant_g6(source: str) -> str:
    """Let `*` cross segment boundaries."""
    return _substitute(source, '"[^/]*".join(', '".*".join(', "G6")


def _mutant_g7(source: str) -> str:
    """Decide a subtree deny by string prefix instead of glob match."""
    return _substitute(
        source, "    pattern = _edit_glob_regex(deny_path)\n",
        '    if deny_path.endswith("/**"):\n'
        '        return allow_path.startswith(deny_path[: -len("/**")])\n'
        "    pattern = _edit_glob_regex(deny_path)\n",
        "G7",
    )


def _mutant_g8(source: str) -> str:
    """Name the two rules in the refusal but not their sources."""
    return _substitute(
        source, _REFUSAL_SOURCES, '                f"is entirely covered by Edit deny {deny_rule!r} "\n', "G8"
    )


def _mutant_g9(source: str) -> str:
    """Check only subtree (add_dir-shaped) allows."""
    return _substitute(
        source, "    for allow_rule in allow:\n",
        '    for allow_rule in [rule for rule in allow if rule.endswith("/**)")]:\n',
        "G9",
    )


def _mutant_g10(source: str) -> str:
    """Let a non-`/**` deny cover a subtree allow."""
    return _substitute(source, _NON_SUBTREE_DENY_EXIT, "", "G10")


def _mutant_g11(source: str) -> str:
    """Emit the repo-root guard denies for every kind, not only developer."""
    return _substitute(source, _REPO_ROOT_KIND_GATE, '    of whether root strictly exceeds cwd."""\n', "G11")


def _mutant_g12(source: str) -> str:
    """Record both sides' sources in one shared map, first rule text wins."""
    return _substitute(source, _DENY_SOURCE_MAP, "    deny_source = allow_source\n", "G12")


def _mutant_g13(source: str) -> str:
    """Refuse a pair from the project's own settings like any other pair."""
    return _substitute(source, _PROJECT_PAIR_TEST, "            if False:\n", "G13")


def _mutant_g14(source: str) -> str:
    """Exempt a pair when EITHER side comes from project settings."""
    return _substitute(
        source, _PROJECT_PAIR_TEST,
        "            if allow_from == PROJECT_SETTINGS_SOURCE or deny_from == PROJECT_SETTINGS_SOURCE:\n",
        "G14",
    )


def _mutant_g15(source: str) -> str:
    """Exempt the project's own pair silently, with no warning."""
    return _substitute(source, _PROJECT_PAIR_WARNING, "", "G15")


_SWEEP_STAGE2_IMAGE_LINE = "    scripts/tests/test_spawn_stage2_image.py\n"
_SWEEP_STAGE_GRANTS_LINE = "    scripts/tests/test_stage_grants.py\n"
_SWEEP_GREEN_REGEX = (
    "    local green='^[0-9]+ passed(, [0-9]+ warnings?)? in [0-9]+\\.[0-9]+s"
    "( \\([0-9]+:[0-9]{2}:[0-9]{2}\\))?$'\n"
)
_SWEEP_GREEN_BODY = _SWEEP_GREEN_REGEX + '    [[ "$1" =~ $green ]]\n'
_SWEEP_REQUIRED_DEFS = (
    "REQUIRED_DEFS=(\n"
    '    "scripts/tests/test_spawn_specialist_grants.py:test_mutation_control_is_collected_and_discriminates"\n'
    ")\n"
)
_SWEEP_MAIN_GUARD = '''if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
    main "$@"
    exit $?
fi
'''


def _mutant_w1(source: str) -> str:
    """Drop one file the membership law requires."""
    return _substitute(source, _SWEEP_STAGE2_IMAGE_LINE, "", "W1")


def _mutant_w2(source: str) -> str:
    """Accept any summary line that starts with a pass count."""
    return _substitute(source, _SWEEP_GREEN_REGEX, "    local green='^[0-9]+ passed'\n", "W2")


def _mutant_w3(source: str) -> str:
    """Require no test to be present."""
    return _substitute(source, _SWEEP_REQUIRED_DEFS, "REQUIRED_DEFS=()\n", "W3")


def _mutant_w4(source: str) -> str:
    """Run main even when the script is sourced."""
    return _substitute(source, _SWEEP_MAIN_GUARD, 'main "$@"\n', "W4")


def _mutant_w5(source: str) -> str:
    """List a file that does not exist."""
    return _substitute(
        source, _SWEEP_STAGE_GRANTS_LINE,
        _SWEEP_STAGE_GRANTS_LINE + "    scripts/tests/test_no_such_file.py\n", "W5",
    )


def _mutant_w6(source: str) -> str:
    """Reject every summary line."""
    return _substitute(source, _SWEEP_GREEN_BODY, "    return 1\n", "W6")


def _mutant_w7(source: str) -> str:
    """List one file twice."""
    return _substitute(source, _SWEEP_STAGE_GRANTS_LINE, _SWEEP_STAGE_GRANTS_LINE * 2, "W7")


def _mutant_w8(source: str) -> str:
    """Exit 0 from main without running anything."""
    return _substitute(source, "main() {\n", "main() {\n    return 0\n", "W8")


def _unmutated(source: str) -> str:
    return source


def _s0(source: str) -> str:
    """The module just before the first collision fix."""
    return _pre_marker_source(source, FIX_MARKER)


def _s0_gs(source: str) -> str:
    """The module just before the guard helper and the final coverage check."""
    return _pre_marker_source(source, GUARD_FIX_MARKER)


def _no_sweep(_source: str) -> None:
    return None


def _gs(*numbers: int) -> set[str]:
    return {f"gs{n}" for n in numbers}


# (name, "module" or "sweep" -- which FIXED file the builder rewrites,
# builder, required-red case set). A builder returning None removes the
# file from the subject's tree. An EMPTY set means every case must be
# GREEN, not merely one outside the set.
SUBJECTS: list[tuple[str, str, Callable[[str], Optional[str]], set[str]]] = [
    ("FIXED", "module", _unmutated, set()),
    ("S0", "module", _s0, {"gc1", "gc7", "gc8", "gc9", "gc10", "gc12", "gc13", "gc14", "gc15", "gc16", "gc19"}),
    ("M1", "module", _mutant_m1, {"gc4", "gc6", "gc11", "gc17"}),
    ("M2", "module", _mutant_m2, {"gc6", "gc9", "gc11", "gc12", "gc13", "gc19"}),
    ("M3", "module", _mutant_m3, {"gc1", "gc5", "gc7", "gc8", "gc9", "gc10", "gc12", "gc13", "gc15", "gc19"}),
    ("M4", "module", _mutant_m4, {"gc2", "gc7", "gc8", "gc9", "gc10", "gc12", "gc13", "gc15", "gc16", "gc19"}),
    ("M5", "module", _mutant_m5, {"gc3", "gc18"}),
    ("M6", "module", _mutant_m6, {"gc8", "gc12", "gc13"}),
    ("M7", "module", _mutant_m7, {"gc11"}),
    ("M8", "module", _mutant_m8, {"gc11"}),
    ("M9", "module", _mutant_m9, {"gc10", "gc15"}),
    ("M10", "module", _mutant_m10, {"gc14"}),
    ("M11", "module", _mutant_m11, {"gc9", "gc12", "gc13", "gc19"}),
    ("M12", "module", _mutant_m12, {"gc17"}),
    ("M13", "module", _mutant_m13, {"gc16"}),
    ("M14", "module", _mutant_m14, {"gc12", "gc13"}),
    ("M15", "module", _mutant_m15, {"gc19"}),
    ("M16", "module", _mutant_m16, {"gc16", "gc18"}),
    ("M17", "module", _mutant_m17, {"gc9"}),
    ("S0_GS", "module", _s0_gs, _gs(1, 2, 5, 9, 10, 11)),
    ("G1", "module", _mutant_g1, _gs(1)),
    ("G2", "module", _mutant_g2, _gs(2, 5, 9, 10, 11)),
    ("G3", "module", _mutant_g3, _gs(3, 4, 6)),
    ("G4", "module", _mutant_g4, _gs(9)),
    ("G5", "module", _mutant_g5, _gs(2)),
    ("G6", "module", _mutant_g6, _gs(7)),
    ("G7", "module", _mutant_g7, _gs(8, 2)),
    ("G8", "module", _mutant_g8, _gs(10, 11)),
    ("G9", "module", _mutant_g9, _gs(2, 11)),
    ("G10", "module", _mutant_g10, _gs(6)),
    ("G11", "module", _mutant_g11, _gs(12)),
    ("G12", "module", _mutant_g12, _gs(13)),
    ("G13", "module", _mutant_g13, _gs(14)),
    ("G14", "module", _mutant_g14, _gs(2, 5, 13, 15)),
    ("G15", "module", _mutant_g15, _gs(14)),
    ("S0_GW", "sweep", _no_sweep, {f"gw{n}" for n in range(1, 9)}),
    ("W1", "sweep", _mutant_w1, {"gw3"}),
    ("W2", "sweep", _mutant_w2, {"gw5"}),
    ("W3", "sweep", _mutant_w3, {"gw7"}),
    ("W4", "sweep", _mutant_w4, {"gw6"}),
    ("W5", "sweep", _mutant_w5, {"gw1"}),
    ("W6", "sweep", _mutant_w6, {"gw4"}),
    ("W7", "sweep", _mutant_w7, {"gw2"}),
    ("W8", "sweep", _mutant_w8, {"gw8"}),
]


def _ordered(cases: Iterable[str]) -> list[str]:
    return sorted(cases, key=lambda case_id: (FAMILIES.index(_family(case_id)), int(case_id[2:])))


def _build_tree(subject_dir: Path, module_source: str, sweep_source: Optional[str]) -> Path:
    """Lays out a repo-shaped tree -- scripts/spawn-specialist.py plus every
    real scripts/tests/test_*.py, conftest.py and the sweep script -- and
    returns its tests directory. The whole tests directory is copied, not
    only the two case files, because the gw cases check the sweep's file
    list against the tree it sits in."""
    tests_dir = subject_dir / "scripts" / "tests"
    tests_dir.mkdir(parents=True)
    (subject_dir / MODULE_RELPATH).write_text(module_source)
    for source_file in [TESTS_DIR / "conftest.py", *TESTS_DIR.glob("test_*.py")]:
        shutil.copyfile(source_file, tests_dir / source_file.name)
    if sweep_source is not None:
        sweep = subject_dir / SWEEP_RELPATH
        sweep.write_text(sweep_source)
        sweep.chmod(0o755)
    return tests_dir


def _run_subject(tests_dir: Path) -> dict[str, tuple[int, Optional[str]]]:
    """Runs the three case families against the tree at `tests_dir` and
    returns, per case id, (collection count, outcome) where outcome is
    "passed"/"failed"/"error"/"skipped"/None (never collected)."""
    report_path = tests_dir.parent.parent / "report.xml"
    env = dict(os.environ)
    path_parts = [str(SCRIPTS_DIR), str(TESTS_DIR)]
    existing = env.get("PYTHONPATH")
    if existing:
        path_parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(path_parts)

    subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "--tb=no", "-k", " or ".join(CASE_FUNCS.values()),
            f"--junitxml={report_path}", *(str(tests_dir / name) for name in CASE_FILES),
        ],
        cwd=str(tests_dir.parent.parent), env=env, capture_output=True, text=True,
    )

    func_to_case = {func: case_id for case_id, func in CASE_FUNCS.items()}
    counts: dict[str, int] = {}
    status: dict[str, str] = {}
    if report_path.exists():
        tree = ET.parse(report_path)
        for testcase in tree.getroot().iter("testcase"):
            case_id = func_to_case.get(testcase.get("name", ""))
            if case_id is None:
                continue
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
        fixed = {"module": FIXED_MODULE.read_text(), "sweep": FIXED_SWEEP.read_text()}

        for name, target, builder, required in SUBJECTS:
            union |= required
            sources: dict[str, Optional[str]] = dict(fixed)
            try:
                sources[target] = builder(fixed[target])
            except (AnchorError, RuntimeError) as exc:
                ok = False
                lines.append(f"{name} [FAIL]: {exc}")
                continue

            tests_dir = _build_tree(scratch_root / name, sources["module"], sources["sweep"])
            outcomes = _run_subject(tests_dir)

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

        for family in FAMILIES:
            family_cases = {c for c in ALL_CASES if _family(c) == family}
            family_union = {c for c in union if _family(c) == family}
            if family_union != family_cases:
                ok = False
                lines.append(
                    f"CATALOGUE [FAIL]: {family} union of listed cases {_ordered(family_union)} "
                    f"!= {_ordered(family_cases)}"
                )
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
