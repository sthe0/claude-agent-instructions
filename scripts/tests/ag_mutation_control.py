"""Mutation-catalogue negative control for the authoring-time grant check
(`scripts/agentctl/grant_shadow.py` and its seams in submission.py,
render.py and cli.py), run against the 25 cases of
test_authoring_grant_check.py:
  ag1..ag14  `plan_grant_shadow_problems` and the submission / plan-grants
             seams that refuse a grant collision;
  mx1..mx11  `mixed_spelling_advisories` and the plan-grants / submit-plan /
             replan seams that report a mixed spelling without refusing it.

Builds 27 subjects, each a scratch copy of scripts/ under one temp root:
  FIXED  the unmutated tree; every case must pass;
  S0     scripts/ as of the parent of the commit that introduced
         FIX_MARKER into grant_shadow.py (located with a `git log -S`
         pickaxe), with the case file taken from HEAD; every case must go red;
  A1-A14, X1-X11  named wrong fixes, each a list of exact textual
         substitutions on the FIXED tree. An anchor that does not occur
         exactly once is a control failure for that subject, not a pass.

A subject is CAUGHT when every case in its red set FAILED or ERRORED,
collected exactly once, per the run's junit XML. The case file imports
grant_shadow inside each case, so a tree without the module fails every case
individually instead of failing collection.

Exit code is zero iff every subject is caught, FIXED is green, the union of
the red sets (S0 excluded) is all 25 cases, every CASE_FUNCS name is a def in
the case file, and `git status` over scripts/ is byte-identical before and
after the run. Every git call is addressed with `-C` at this checkout's root,
never the caller's cwd.

Run: `python3 scripts/tests/ag_mutation_control.py` from anywhere.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
CASE_FILE_RELPATH = "scripts/tests/test_authoring_grant_check.py"
MARKER_RELPATH = "scripts/agentctl/grant_shadow.py"
FIX_MARKER = "The authoring check calls the spawn-time assembler; it does not restate it."

SUBMISSION = "scripts/agentctl/submission.py"
RENDER = "scripts/agentctl/render.py"
CLI = "scripts/agentctl/cli.py"
SHADOW = MARKER_RELPATH

CASE_FUNCS = {
    "ag1": "test_ag1_collision_reported_with_both_derivations",
    "ag2": "test_ag2_problem_text_is_build_child_settings_refusal",
    "ag3": "test_ag3_clean_stage_has_no_problem",
    "ag4": "test_ag4_declared_write_add_dir_resolves_collision",
    "ag5": "test_ag5_collision_refused_whatever_the_weight_class",
    "ag6": "test_ag6_plan_grants_exits_one_on_collision",
    "ag7": "test_ag7_plan_grants_json_carries_shadow_problems",
    "ag8": "test_ag8_in_thread_stage_is_not_checked",
    "ag9": "test_ag9_every_spawn_stage_is_checked",
    "ag10": "test_ag10_workdir_is_the_plan_venue_not_the_authors_cwd",
    "ag11": "test_ag11_cli_submission_seam_returns_the_problem",
    "ag12": "test_ag12_invalid_add_dir_is_a_problem_not_a_raise",
    "ag13": "test_ag13_uncreated_delivery_worktree_is_not_replaced_by_repo_root",
    "ag14": "test_ag14_repo_root_guard_deny_collision_names_its_source",
    "mx1": "test_mx1_same_file_both_spellings",
    "mx2": "test_mx2_two_files_same_directory",
    "mx3": "test_mx3_knowledge_refs_count",
    "mx4": "test_mx4_uniform_spelling_is_silent",
    "mx5": "test_mx5_pairs_are_per_stage",
    "mx6": "test_mx6_directory_is_compared_by_segment",
    "mx7": "test_mx7_advisory_is_not_a_submission_problem",
    "mx8": "test_mx8_plan_grants_prints_advisory_and_exits_zero",
    "mx9": "test_mx9_submit_plan_carries_advisory",
    "mx10": "test_mx10_replan_carries_advisory",
    "mx11": "test_mx11_nested_directory_counts_and_collides",
}
ALL_CASES = set(CASE_FUNCS)

_SUBMIT_CALL = "    out.extend(grant_shadow.plan_grant_shadow_problems(doc))\n"
_APPEND_LINE = '            problems.append(f"stage {s.index} ({s.title}): {exc}")\n'
_LOOP_END = _APPEND_LINE + "    return problems"

Substitution = tuple[str, str, str]  # (relpath, anchor, replacement)

# (name, substitutions, red set)
MUTANTS: list[tuple[str, list[Substitution], set[str]]] = [
    ("A1", [(SUBMISSION, _SUBMIT_CALL, "")], {"ag5", "ag11"}),
    ("A2", [
        (SUBMISSION, _SUBMIT_CALL, ""),
        (SUBMISSION, "        return out\n    # Plan-level first",
         "        return out\n" + _SUBMIT_CALL + "    # Plan-level first"),
    ], {"ag5"}),
    ("A3", [(RENDER, "    problems = grant_shadow.plan_grant_shadow_problems(doc)", "    problems = []")],
     {"ag6", "ag7"}),
    ("A4", [(SHADOW, "        except (spawn.GrantShadowError, _grants.GrantValidationError) as exc:\n",
             "        except spawn.GrantShadowError:\n            continue\n"
             "        except _grants.GrantValidationError as exc:\n")],
     {"ag1", "ag2", "ag6", "ag9"}),
    ("A5", [(SHADOW, 'f"stage {s.index} ({s.title}): {exc}"',
             'f"stage {s.index} ({s.title}): grant collision: {exc}"')], {"ag2"}),
    ("A6", [(SHADOW,
             "        entries += [r.to_dict() for r in derived.allow] + [a.to_dict() for a in derived.add_dirs]\n",
             "")], {"ag1", "ag9"}),
    ("A7", [(SHADOW,
             "        entries = [r.to_dict() for r in declared.allow] + [a.to_dict() for a in declared.add_dirs]\n",
             "        entries = []\n")], {"ag4"}),
    ("A8", [(SHADOW, "    workdir = _dispatch_workdir(doc)\n", "    workdir = os.getcwd()\n")], {"ag10"}),
    ("A9", [(SHADOW, "        if not s.is_spawn():\n            continue\n", "")], {"ag8"}),
    ("A10", [(SHADOW, _LOOP_END, _APPEND_LINE + "        break\n    return problems")], {"ag9"}),
    ("A11", [(SHADOW, "        except (spawn.GrantShadowError, _grants.GrantValidationError) as exc:\n",
              "        except spawn.GrantShadowError as exc:\n")], {"ag12"}),
    ("A12", [(SHADOW, _LOOP_END,
              _APPEND_LINE + "        else:\n"
              '            problems.append(f"stage {s.index} ({s.title}): clean")\n'
              "    return problems")], {"ag3"}),
    ("A13", [(SHADOW, "    return doc.meta.delivery_worktree or doc.meta.repo_root or None\n",
              "    worktree = doc.meta.delivery_worktree\n"
              "    if worktree and not os.path.isdir(worktree):\n"
              "        worktree = None\n"
              "    return worktree or doc.meta.repo_root or None\n")], {"ag13"}),
    ("A14", [(SHADOW, "    workdir = _dispatch_workdir(doc)\n", "    workdir = None\n")], {"ag14"}),
    ("X1", [(SHADOW, '            ("knowledge_refs", s.subject.knowledge_refs),\n', "")], {"mx3"}),
    ("X2", [(SHADOW, "    under = resolved == directory or directory in resolved.parents\n",
             "    under = resolved == PurePosixPath(os.path.normpath(absolute[0]))\n")], {"mx2"}),
    ("X3", [(SHADOW, "        refs = _stage_refs(s)\n",
             "        refs = [r for t in doc.stages for r in _stage_refs(t)]\n")], {"mx5"}),
    ("X4", [(SUBMISSION, _SUBMIT_CALL,
             "    out.extend(grant_shadow.plan_grant_shadow_problems(doc)"
             " + grant_shadow.mixed_spelling_advisories(doc))\n")], {"mx7"}),
    ("X5", [(SHADOW, "    under = resolved == directory or directory in resolved.parents\n",
             "    under = str(resolved).startswith(str(directory))\n")], {"mx6"}),
    ("X6", [(SHADOW, "    resolved = PurePosixPath(os.path.normpath(os.path.join(venue, relative[0])))\n",
             "    resolved = PurePosixPath(os.path.normpath(relative[0]))\n")], {"mx1"}),
    ("X7", [(CLI, "    advisories = _replan_spelling_advisories(args.plan)\n"
             "    if advisories:\n"
             '        d.data.setdefault("advisories", []).extend(advisories)\n',
             "")], {"mx10"}),
    ("X8", [(RENDER, "    ok = not problems\n", "    ok = not (problems or advisories)\n")], {"mx8"}),
    ("X9", [(SHADOW, '    if first[0].startswith("/") == second[0].startswith("/"):\n        return None\n',
             "    return first, second\n")], {"mx4", "mx6"}),
    ("X10", [(CLI, '    d.data.setdefault("advisories", []).extend(grant_shadow.mixed_spelling_advisories(doc))\n',
              "")], {"mx9"}),
    ("X11", [(SHADOW, "    under = resolved == directory or directory in resolved.parents\n",
              "    under = resolved.parent == directory\n")], {"mx11"}),
]


class AnchorError(RuntimeError):
    pass


def _git(*args: str, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=not binary)


def _git_status() -> bytes:
    return _git("status", "--porcelain", "--untracked-files=all", "--", ":(top)scripts", binary=True).stdout


def _substitute(source: str, anchor: str, replacement: str, label: str) -> str:
    count = source.count(anchor)
    if count != 1:
        raise AnchorError(f"{label}: anchor occurs {count} times, expected exactly 1: {anchor!r}")
    return source.replace(anchor, replacement, 1)


def _copy_scripts(dest_root: Path) -> None:
    shutil.copytree(SCRIPTS_DIR, dest_root / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    # agentctl.config resolves config.md as scripts/../config.md, so a subject
    # root holding only scripts/ makes every case that reaches cmd_classify die
    # on a missing file — red for a reason no mutant produced.
    shutil.copyfile(ROOT / "config.md", dest_root / "config.md")


def _build_mutant(dest_root: Path, substitutions: list[Substitution], name: str) -> None:
    _copy_scripts(dest_root)
    for relpath, anchor, replacement in substitutions:
        target = dest_root / relpath
        target.write_text(_substitute(target.read_text(), anchor, replacement, f"{name} {relpath}"))


def _build_s0(dest_root: Path) -> None:
    log = _git("log", "--reverse", "--format=%H", "-S", FIX_MARKER, "--", MARKER_RELPATH)
    shas = log.stdout.split()
    if log.returncode != 0 or not shas:
        raise RuntimeError(f"S0: no commit introduces the fix marker into {MARKER_RELPATH}")
    archive = _git("archive", "--format=tar", f"{shas[0]}^", "scripts", "config.md", binary=True)
    if archive.returncode != 0:
        raise RuntimeError(f"S0: git archive of {shas[0]}^ failed: {archive.stderr!r}")
    dest_root.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest_root, filter="data")
    case_file = _git("show", f"HEAD:{CASE_FILE_RELPATH}")
    if case_file.returncode != 0:
        raise RuntimeError(f"S0: {CASE_FILE_RELPATH} is not in HEAD")
    (dest_root / CASE_FILE_RELPATH).write_text(case_file.stdout)


def _run_subject(subject_root: Path) -> dict[str, tuple[int, Optional[str]]]:
    """Per case id: (times collected, "passed"/"failed"/"error"/"skipped"/None)."""
    report = subject_root / "report.xml"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(subject_root / "scripts"), str(subject_root / "scripts" / "tests")])
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider",
         "-k", " or ".join(CASE_FUNCS.values()), f"--junitxml={report}",
         str(subject_root / CASE_FILE_RELPATH)],
        cwd=str(subject_root), env=env, capture_output=True, text=True,
    )
    func_to_case = {func: case for case, func in CASE_FUNCS.items()}
    counts: dict[str, int] = {}
    status: dict[str, str] = {}
    if report.exists():
        for testcase in ET.parse(report).getroot().iter("testcase"):
            case = func_to_case.get(testcase.get("name", ""))
            if case is None:
                continue
            counts[case] = counts.get(case, 0) + 1
            if testcase.find("failure") is not None:
                status[case] = "failed"
            elif testcase.find("error") is not None:
                status[case] = "error"
            elif testcase.find("skipped") is not None:
                status[case] = "skipped"
            else:
                status[case] = "passed"
    return {case: (counts.get(case, 0), status.get(case)) for case in ALL_CASES}


def _ordered(cases) -> list[str]:
    return sorted(cases, key=lambda c: (c[:2], int(c[2:])))


def _red(outcome: tuple[int, Optional[str]]) -> bool:
    return outcome[0] == 1 and outcome[1] in ("failed", "error")


def _green(outcome: tuple[int, Optional[str]]) -> bool:
    return outcome == (1, "passed")


def _self_check_case_funcs() -> list[str]:
    source = (ROOT / CASE_FILE_RELPATH).read_text()
    return [
        f"CASE_FUNCS [FAIL]: {case}={func} is not a def in {CASE_FILE_RELPATH}"
        for case, func in CASE_FUNCS.items()
        if not re.search(rf"^def {re.escape(func)}\(", source, re.MULTILINE)
    ]


def main() -> int:
    before = _git_status()
    lines = _self_check_case_funcs()
    ok = not lines

    union = set().union(*(red for _name, _subs, red in MUTANTS))
    if union != ALL_CASES:
        ok = False
        lines.append(f"CATALOGUE [FAIL]: red sets cover {_ordered(union)}, missing {_ordered(ALL_CASES - union)}")

    scratch = Path(tempfile.mkdtemp(prefix="ag-mutation-control-"))
    try:
        fixed_root = scratch / "FIXED"
        _copy_scripts(fixed_root)
        fixed = _run_subject(fixed_root)
        not_green = [c for c in _ordered(ALL_CASES) if not _green(fixed[c])]
        ok = ok and not not_green
        lines.append(f"FIXED [{'FAIL' if not_green else 'OK'}]" + (f": not_green={not_green}" if not_green else ""))

        builders = [("S0", lambda root: _build_s0(root), ALL_CASES)]
        builders += [(name, (lambda root, n=name, s=subs: _build_mutant(root, s, n)), red)
                     for name, subs, red in MUTANTS]
        for name, build, red in builders:
            root = scratch / name
            try:
                build(root)
            except (AnchorError, RuntimeError, OSError) as exc:
                ok = False
                lines.append(f"{name} [FAIL]: {exc}")
                continue
            outcomes = _run_subject(root)
            missing = [c for c in _ordered(red) if not _red(outcomes[c])]
            ok = ok and not missing
            reddened = [c for c in _ordered(ALL_CASES) if _red(outcomes[c])]
            word = "FAIL" if missing else "OK"
            extra = f" missing_red={missing}" if missing else ""
            lines.append(f"{name} [{word}]: required={_ordered(red)} red={reddened}{extra}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if _git_status() != before:
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
