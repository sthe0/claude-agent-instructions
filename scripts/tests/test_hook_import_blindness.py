"""A judge-calling hook that dies importing its own dependencies must leave a
DISTINGUISHABLE `import_failed` ledger line, not the same silence as the
engine path (which never writes `hook_start` either).

Covers the stage's done criterion end to end:
  1. a real poisoned-import subprocess run of a judge-calling hook writes
     import_failed to the ledger, naming the hook and the exception;
  2. that poisoning changes NEITHER the hook's exit code NOR its stderr
     (modulo the one frame legitimately allowed to shift -- see below),
     compared against the pinned pre-fix revision of the same file poisoned
     the same way -- the fix must be silent to the outside;
  3. an unpoisoned run of the same hook never writes import_failed;
  4. an engine-path invocation (no hook_start line at all) still classifies as
     None, not outcome 12 -- and outcome 12 itself is fail_open and outside
     NOT_FAIL_OPEN_IDS;
  5. check-dispatch-witness.py, run on synthetic fixtures covering its full
     required argument set, produces no traceback;
  6. the other two judge-calling hooks wrap the same imports in the same
     except-BaseException / import_failed / bare-raise shape (structural
     parity, checked without paying for two more real subprocess runs).

Poisoning never touches the real scripts/ tree. Every live judge-calling hook
on this machine imports from that tree by absolute path, so corrupting a file
there for even the ~0.2s a subprocess call takes would risk a concurrent live
hook dying for real and writing a bogus import_failed line into the live
judge ledger. Instead, each poisoning test copies the scripts/ tree (minus
__pycache__ and the heavy tests/ directory) into a pytest tmp_path and
corrupts the copy's outage_escalation_detect.py -- the copy is thrown away
with tmp_path, so a crash mid-test leaves no trace in the real tree either.

The "pre-fix" comparison in test 2 runs the PRE_FIX_REVISION's source of the
hook from a second file placed in that SAME poisoned copy directory (not the
real hook file, and not a scratch file in the real scripts/ tree), so its
sibling imports resolve against the identical poisoned module the post-fix
run uses. PRE_FIX_REVISION is a commit literal, not HEAD: HEAD moves once
Stage 6 commits this very fix onto this branch, at which point comparing
against HEAD would silently compare the fix against itself.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from lib import judge_ledger  # noqa: E402

HOOK_NAME = "hook-escalation-diagnosis-gate.py"
HOOK = SCRIPTS_DIR / HOOK_NAME
POISON_TARGET_NAME = "outage_escalation_detect.py"
DISPATCH_WITNESS = SCRIPTS_DIR / "check-dispatch-witness.py"

OTHER_HOOKS = {
    "deferring_disposition": SCRIPTS_DIR / "hook-deferring-disposition-gate.py",
    "turn_end": SCRIPTS_DIR / "hook-turn-end-gate.py",
}

# The judge-import-blindness branch's own base commit -- this worktree's HEAD
# before Stage 2's edit lands the fix (the plan's own literal throughout:
# "ПРОГНАНО АВТОРОМ НА БАЗЕ ... HEAD eadd312"). Pinned to a commit, not HEAD,
# so that once Stage 6 commits the fix onto this branch, "pre-fix" and
# "post-fix" don't silently collapse onto the same revision.
PRE_FIX_REVISION = "eadd312"

_POISON_SYNTAX_ERROR_SOURCE = "def broken(:\n"  # deliberately invalid syntax

_HOOK_FRAME_LINE_RE = re.compile(r'(File "<HOOK>", line )\d+(, in <module>)')


def _load_script(filename: str):
    spec = importlib.util.spec_from_file_location(
        filename.replace("-", "_").replace(".py", "").replace("/", "_"),
        SCRIPTS_DIR / filename,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_report = _load_script("judge-usage-report.py")


def _poisoned_scripts_copy(dest_dir: Path) -> Path:
    """Copy the scripts/ tree (minus __pycache__ and the heavy tests/ dir)
    into dest_dir and corrupt the copy's POISON_TARGET_NAME with invalid
    syntax, so poisoning an import never touches the real scripts/ tree a
    live judge-calling hook on this machine might be mid-import against."""
    copy_root = dest_dir / "poisoned_scripts_copy"
    shutil.copytree(
        SCRIPTS_DIR,
        copy_root,
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )
    (copy_root / POISON_TARGET_NAME).write_text(_POISON_SYNTAX_ERROR_SOURCE, encoding="utf-8")
    return copy_root


def _run(path: Path, *, stdin: str = "", ledger: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if ledger is not None:
        env["AGENTCTL_JUDGE_LEDGER"] = str(ledger)
    return subprocess.run(
        [sys.executable, str(path)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


def _normalize_stderr(stderr: str, hook_path: Path) -> str:
    """Redact the one frame allowed to legitimately differ between the
    pre-fix and post-fix runs -- the outer hook script's own file path and
    the line number of its now-wrapped import statement -- so the comparison
    still catches anything else: an unexpected extra frame, a changed
    exception type or message, a changed poisoned-module frame."""
    text = stderr.replace(str(hook_path), "<HOOK>")
    return _HOOK_FRAME_LINE_RE.sub(r"\1<N>\2", text)


# --- 1. a poisoned real process writes import_failed --------------------------

def test_poisoned_import_writes_import_failed_to_ledger(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    copy_root = _poisoned_scripts_copy(tmp_path)

    result = _run(copy_root / HOOK_NAME, ledger=ledger)

    assert result.returncode != 0  # the hook must still die the same way it always did
    records = judge_ledger.read_records(ledger)
    import_failed = [r for r in records if r.get("kind") == "import_failed"]
    assert len(import_failed) == 1
    assert import_failed[0]["hook"] == "escalation_diagnosis"
    assert "outage_escalation_detect" in import_failed[0]["reason"]
    # no hook_start line exists -- the process died before reaching it
    assert not any(r.get("kind") == "hook_start" for r in records)


# --- 2. poisoning is silent: exit code and stderr unchanged vs. pre-fix --------

def test_poisoned_import_exit_code_and_stderr_unchanged_vs_pre_fix(tmp_path):
    pre_fix_source = subprocess.run(
        ["git", "show", f"{PRE_FIX_REVISION}:scripts/{HOOK_NAME}"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert "import_failed" not in pre_fix_source, (
        f"{PRE_FIX_REVISION} already carries the fix -- re-point PRE_FIX_REVISION "
        "at a commit before Stage 2's edit"
    )

    copy_root = _poisoned_scripts_copy(tmp_path)
    post_hook = copy_root / HOOK_NAME
    pre_hook = copy_root / "_pre_fix_hook_escalation_diagnosis_gate.py"
    pre_hook.write_text(pre_fix_source, encoding="utf-8")

    pre = _run(pre_hook, ledger=tmp_path / "pre.jsonl")
    post = _run(post_hook, ledger=tmp_path / "post.jsonl")

    # Non-vacuity, asserted here rather than borrowed transitively from test 1:
    # if the poisoning silently stopped working, both runs would exit 0 with an
    # empty stderr and the two equalities below would hold for the wrong reason.
    assert pre.returncode != 0
    assert pre.returncode == post.returncode
    assert _normalize_stderr(pre.stderr, pre_hook) == _normalize_stderr(post.stderr, post_hook)


# --- 3. a clean run never writes import_failed ---------------------------------

def test_clean_run_writes_no_import_failed(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    result = _run(HOOK, stdin=json.dumps({"tool_name": "Bash"}), ledger=ledger)

    assert result.returncode == 0
    records = judge_ledger.read_records(ledger)
    assert any(r.get("kind") == "hook_start" for r in records)
    assert not any(r.get("kind") == "import_failed" for r in records)


# --- 4. the engine path (no hook_start) still classifies as None ---------------

def test_engine_path_without_hook_start_still_classifies_none():
    engine_path_records = [
        {"kind": "started", "judge": "outage_escalation"},
        {"kind": "call", "judge": "outage_escalation", "timed_out": False, "duration": 1.2, "returncode": 0},
    ]
    assert _report.classify_invocation(engine_path_records) is None


def test_outcome_12_is_fail_open_and_outside_not_fail_open_ids():
    assert _report.OUTCOME_BY_ID["12"].fail_open is True
    assert "12" not in _report.NOT_FAIL_OPEN_IDS


# --- 5. the dispatch witness runs clean on synthetic fixtures -------------------

def test_dispatch_witness_synthetic_fixtures_produce_no_traceback(tmp_path):
    since_file = tmp_path / "since.stamp"
    since_file.write_text("", encoding="utf-8")
    old_wiring_file = tmp_path / "old-wiring.json"
    old_wiring_file.write_text(
        json.dumps({"schema": "dispatch-witness-old-wiring/v2", "hooks": {}}),
        encoding="utf-8",
    )
    ledger = tmp_path / "empty-ledger.jsonl"
    ledger.write_text("", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable, str(DISPATCH_WITNESS),
            "--since-file", str(since_file),
            "--session-id", "synthetic-import-blindness-session",
            "--old-wiring-file", str(old_wiring_file),
            "--ledger", str(ledger),
        ],
        capture_output=True, text=True,
    )

    assert "Traceback" not in result.stderr
    assert result.returncode in (0, 1)  # a clean verdict either way, never a crash


# --- 6. structural parity across the other two judge-calling hooks -------------

def test_other_hooks_wrap_their_imports_the_same_way():
    for hook_name, path in OTHER_HOOKS.items():
        source = path.read_text(encoding="utf-8")
        pattern = re.compile(
            r'except BaseException as exc:\n\s*judge_ledger\.import_failed\(\s*"'
            + re.escape(hook_name)
            + r'"\s*,[^\n]*\)\n\s*raise\b'
        )
        assert pattern.search(source), (
            f"{path.name} does not wrap its risky imports in the same "
            f'except-BaseException / judge_ledger.import_failed("{hook_name}", ...) / '
            "bare-raise shape as the fixed escalation-diagnosis hook"
        )
