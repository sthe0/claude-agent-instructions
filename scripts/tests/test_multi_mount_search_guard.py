"""Tests for hook-multi-mount-search-guard.py: deny recursive searches spanning ≥2 FUSE mounts."""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hook-multi-mount-search-guard.py"

spec = importlib.util.spec_from_file_location("hook_multi_mount_search_guard", str(HOOK))
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
decide = _mod.decide
fuse_mounts_from_text = _mod.fuse_mounts_from_text

_PROC_TEXT = """
sysfs /sys sysfs rw 0 0
proc /proc proc rw 0 0
vcsfs /home/the0/monorepo fuse.vcsfs rw 0 0
vcsfs /home/the0/monorepo_local fuse.vcsfs rw 0 0
vcsfs /home/the0/monorepo_PROJ-100 fuse.vcsfs rw 0 0
vcsfs /home/the0/monorepo_PROJ-200 fuse.vcsfs rw 0 0
vcsfs /home/the0/monorepo_PROJ-300 fuse.vcsfs rw 0 0
"""

MOUNTS_5 = fuse_mounts_from_text(_PROC_TEXT)
HOME = "/home/the0"
PROJ = f"{HOME}/claude-agent-instructions"


def _is_deny(reason):
    return reason is not None


@pytest.fixture(autouse=True)
def _hermetic_home(monkeypatch):
    """decide() resolves roots via os.path.expanduser/expandvars/realpath, so on a
    host whose real home is not /home/the0 (macOS: ~ -> /Users/..., /home is a
    symlink into /System/Volumes/Data) every deny assertion below silently stops
    matching the fixture mounts. Pin resolution to the fixture's world."""
    monkeypatch.setenv("HOME", HOME)
    monkeypatch.setattr(_mod.os.path, "expanduser",
                        lambda p: HOME + p[1:] if p.startswith("~") else p)
    monkeypatch.setattr(_mod.os.path, "realpath", lambda p, **kw: p)


# --- fuse_mounts_from_text ---

def test_fuse_mounts_from_text_extracts_fuse_home_only():
    assert len(MOUNTS_5) == 5
    assert all(m.startswith("/home/the0/") for m in MOUNTS_5)


def test_fuse_mounts_from_text_octal_decode():
    text = "vcsfs /home/the0/dir\\040with\\040spaces fuse.vcsfs rw 0 0\n"
    mounts = fuse_mounts_from_text(text)
    assert mounts == ["/home/the0/dir with spaces"]


# --- Grep tool ---

def test_grep_rooted_at_home_deny():
    r = decide("Grep", {"path": HOME}, HOME, MOUNTS_5)
    assert _is_deny(r)
    assert HOME in r


def test_grep_path_omitted_cwd_is_home_deny():
    r = decide("Grep", {}, HOME, MOUNTS_5)
    assert _is_deny(r)


def test_grep_rooted_in_project_allow():
    r = decide("Grep", {"path": PROJ}, PROJ, MOUNTS_5)
    assert r is None


# --- Glob tool ---

def test_glob_tilde_deny():
    r = decide("Glob", {"path": "~"}, HOME, MOUNTS_5)
    assert _is_deny(r)


def test_glob_rooted_in_project_allow():
    r = decide("Glob", {"path": PROJ}, PROJ, MOUNTS_5)
    assert r is None


# --- Bash: find ---

def test_bash_find_home_deny():
    r = decide("Bash", {"command": f"find {HOME} -name '*.py'"}, HOME, MOUNTS_5)
    assert _is_deny(r)


def test_bash_find_tilde_deny():
    r = decide("Bash", {"command": "find ~ -name '*.py'"}, HOME, MOUNTS_5)
    assert _is_deny(r)


def test_bash_find_in_project_allow():
    r = decide("Bash", {"command": f"find {PROJ} -name '*.py'"}, PROJ, MOUNTS_5)
    assert r is None


# --- Bash: rg without explicit path → uses cwd ---

def test_bash_rg_no_path_cwd_home_deny():
    r = decide("Bash", {"command": "rg foo"}, HOME, MOUNTS_5)
    assert _is_deny(r)


def test_bash_rg_no_path_cwd_project_allow():
    r = decide("Bash", {"command": "rg foo"}, PROJ, MOUNTS_5)
    assert r is None


# --- Bash: grep -rn inside a single mount → ALLOW ---

def test_bash_grep_rn_inside_single_mount_allow():
    single_root = f"{HOME}/monorepo_local/sub"  # inside exactly one of MOUNTS_5
    r = decide("Bash", {"command": f"grep -rn foo {single_root}"}, single_root, MOUNTS_5)
    assert r is None


# --- Bash: non-search commands → ALLOW ---

def test_bash_echo_allow():
    r = decide("Bash", {"command": "echo hi"}, HOME, MOUNTS_5)
    assert r is None


def test_bash_git_status_allow():
    r = decide("Bash", {"command": "git status"}, HOME, MOUNTS_5)
    assert r is None


# --- No FUSE mounts → ALLOW regardless ---

def test_no_mounts_grep_allow():
    r = decide("Grep", {"path": HOME}, HOME, [])
    assert r is None


def test_no_mounts_bash_find_allow():
    r = decide("Bash", {"command": f"find {HOME} -name x"}, HOME, [])
    assert r is None


# --- Missing / empty inputs → no exception ---

def test_decide_missing_command_allow():
    r = decide("Bash", {}, HOME, MOUNTS_5)
    assert r is None


def test_decide_missing_path_falls_back_to_cwd():
    r = decide("Grep", {}, PROJ, MOUNTS_5)
    assert r is None


def test_decide_unknown_tool_allow():
    r = decide("Edit", {"file_path": HOME}, HOME, MOUNTS_5)
    assert r is None


# --- Subprocess: malformed stdin → exits 0, no output ---

def test_main_malformed_stdin_allows():
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not valid json",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- mount-root / ancestor traversal ---

MR = f"{HOME}/monorepo_local"
ONE = [f"{HOME}/work/monorepo"]
ABOVE = f"{HOME}/work"
OUT = PROJ
_EXISTING = {f"{MR}/robot", f"{MR}/src"}


@pytest.fixture(autouse=True)
def _fake_exists(monkeypatch):
    real = _mod.os.path.exists
    monkeypatch.setattr(_mod.os.path, "exists",
                        lambda p: _mod.os.path.normpath(p) in _EXISTING or real(p))


def _bash(mod, cmd, cwd, mounts):
    return mod.decide("Bash", {"command": cmd}, cwd, mounts)


def _deny(mod, cmd, cwd=OUT, mounts=MOUNTS_5):
    assert _bash(mod, cmd, cwd, mounts) is not None, cmd


def _allow(mod, cmd, cwd=OUT, mounts=MOUNTS_5):
    assert _bash(mod, cmd, cwd, mounts) is None, cmd


DENY_CASES = [
    (f"find {MR} -name x", OUT, MOUNTS_5),
    (f"grep -r x {MR}", OUT, MOUNTS_5),
    (f"rg foo {MR}", OUT, MOUNTS_5),
    (f"fd foo {MR}", OUT, MOUNTS_5),
    (f"ls -R {MR}", OUT, MOUNTS_5),
    (f"du -sh {MR}", OUT, MOUNTS_5),
    ("du", MR, MOUNTS_5),
    (f"lsof +D {MR}", OUT, MOUNTS_5),
    (f"lsof +D{MR}", OUT, MOUNTS_5),
    (f"find {MR}/ -name x", OUT, MOUNTS_5),
    (f"cd {MR} && find .", OUT, MOUNTS_5),
    ("find .", MR, MOUNTS_5),
    (f"find {ABOVE} -name x", OUT, ONE),
    (f"grep -r x {ABOVE}", OUT, ONE),
    (f"du -sh {ABOVE}", OUT, ONE),
    ("rg foo", ABOVE, ONE),
    ("rg src", MR, MOUNTS_5),
    ("grep -r robot", MR, MOUNTS_5),
    ("fd robot", MR, MOUNTS_5),
    ("find -name src", MR, MOUNTS_5),
    ("rg -g '*.py' robot", MR, MOUNTS_5),
    ("rg -t py src", MR, MOUNTS_5),
    ("fd -e py robot", MR, MOUNTS_5),
    ("grep -r -A 2 robot", MR, MOUNTS_5),
    ("grep -rA 2 robot", MR, MOUNTS_5),
    ("du -sh 2>/dev/null", MR, MOUNTS_5),
    ("rg foo 2>/dev/null", MR, MOUNTS_5),
    ("grep -r x > /tmp/out", MR, MOUNTS_5),
    ("rg -- robot", MR, MOUNTS_5),
    (f"cd {MR}\nfind . -name x", OUT, MOUNTS_5),
    (f"cd {MR} &&\nfind .", OUT, MOUNTS_5),
    ("cd && find .", OUT, MOUNTS_5),
    ("cd; du -sh .", OUT, MOUNTS_5),
]

# Documented false positives: pinned so a change in them is noticed, not required.
ACCEPTED_FALSE_POSITIVES = [
    ("which du", MR, MOUNTS_5),
]

ALLOW_CASES = [
    (f"find {MR}/sub -name x", OUT, MOUNTS_5),
    (f"grep -r x {MR}/sub", OUT, MOUNTS_5),
    (f"rg foo {MR}/sub", OUT, MOUNTS_5),
    (f"fd foo {MR}/sub", OUT, MOUNTS_5),
    (f"ls -R {MR}/sub", OUT, MOUNTS_5),
    (f"du -sh {MR}/sub", OUT, MOUNTS_5),
    (f"lsof +D {MR}/sub", OUT, MOUNTS_5),
    (f"lsof +D{MR}/sub", OUT, MOUNTS_5),
    (f"cd {MR} && rg foo robot/", OUT, MOUNTS_5),
    ("grep -r foo robot", MR, MOUNTS_5),
    ("rg foo src/", MR, MOUNTS_5),
    ("rg -e src robot", MR, MOUNTS_5),
    ("rg -g '*.py' foo robot", MR, MOUNTS_5),
    ("find -L robot -name x", MR, MOUNTS_5),
    ("rg foo robot 2>/dev/null", MR, MOUNTS_5),
    ("rg foo src/ > /tmp/o", MR, MOUNTS_5),
    (f"ls {MR}; find sub -name x", OUT, MOUNTS_5),
    (f"du -sh sub; ls {MR}", OUT, MOUNTS_5),
    (f"fuser -m {MR}", OUT, MOUNTS_5),
    (f"fuser -m {MR}", MR, MOUNTS_5),
    (f"lsof -D {MR}", OUT, MOUNTS_5),
    ("lsof -p 1", OUT, MOUNTS_5),
    (f"lsof {MR}/file", OUT, MOUNTS_5),
    (f"ls {MR}", OUT, MOUNTS_5),
    (f"find {MR} -name x", OUT, []),
]


@pytest.mark.parametrize("cmd,cwd,mounts", DENY_CASES)
def test_bash_deny(cmd, cwd, mounts):
    _deny(_mod, cmd, cwd, mounts)


@pytest.mark.parametrize("cmd,cwd,mounts", ALLOW_CASES)
def test_bash_allow(cmd, cwd, mounts):
    _allow(_mod, cmd, cwd, mounts)


@pytest.mark.parametrize("cmd,cwd,mounts", ACCEPTED_FALSE_POSITIVES)
def test_bash_accepted_false_positive(cmd, cwd, mounts):
    _deny(_mod, cmd, cwd, mounts)


def test_grep_glob_tools_mount_root_and_ancestor_deny():
    for tool in ("Grep", "Glob"):
        assert _mod.decide(tool, {"path": MR}, OUT, MOUNTS_5) is not None
        assert _mod.decide(tool, {}, MR, MOUNTS_5) is not None
        assert _mod.decide(tool, {"path": ABOVE}, OUT, ONE) is not None
        assert _mod.decide(tool, {"path": f"{MR}/sub"}, OUT, MOUNTS_5) is None


def test_deny_message_names_root_subdirectory_and_fuser():
    r = _bash(_mod, f"du -sh {MR}", OUT, MOUNTS_5)
    assert MR in r and "subdirectory" in r and "fuser -m" in r


def test_multi_mount_message_wording_kept():
    r = _mod.decide("Grep", {"path": HOME}, HOME, MOUNTS_5)
    assert "spans 5 FUSE mounts" in r


# --- main(): in-process deny and fail-open ---

def _run_main(mod, monkeypatch, capsys, stdin_text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(mod, "fuse_mounts", lambda: MOUNTS_5)
    rc = mod.main()
    return rc, capsys.readouterr().out


def test_main_denies_in_process(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "cwd": OUT, "tool_input": {"command": f"du -sh {MR}"}}
    rc, out = _run_main(_mod, monkeypatch, capsys, json.dumps(payload))
    assert rc == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("stdin_text", [
    "[]", '"x"',
    json.dumps({"tool_name": "Bash", "tool_input": ["du"]}),
])
def test_main_fail_open_on_odd_payloads(monkeypatch, capsys, stdin_text):
    assert _run_main(_mod, monkeypatch, capsys, stdin_text) == (0, "")


def test_main_fail_open_when_decide_raises(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(_mod, "decide", boom)
    payload = {"tool_name": "Bash", "tool_input": {"command": "du"}}
    assert _run_main(_mod, monkeypatch, capsys, json.dumps(payload)) == (0, "")


# --- mutation catalogue: each mutant must flip its named case ---

def _mutant(old, new):
    text = HOOK.read_text()
    assert old in text, old
    mod = types.ModuleType("mutant_hook")
    mod.__file__ = str(HOOK)
    exec(compile(text.replace(old, new), str(HOOK), "exec"), mod.__dict__)
    return mod


def _flips(case, *args):
    with pytest.raises(AssertionError):
        case(*args)


def test_m1_revert_to_span_two():
    m = _mutant("n >= 1", "n >= 2")
    _flips(_deny, m, f"find {MR} -name x")


def test_m2_drop_du():
    m = _mutant('["find", "rg", "fd", "du"]', '["find", "rg", "fd"]')
    _flips(_deny, m, f"du -sh {MR}")


def test_m3_drop_lsof_plus_d():
    m = _mutant('any(a.startswith("+D") for a in rest)', "False")
    _flips(_deny, m, f"lsof +D {MR}")
    _flips(_deny, m, f"lsof +D{MR}")


def test_m4_subdirectory_regression():
    m = _mutant('m == norm or m.startswith(norm + "/")',
                'm == norm or m.startswith(norm + "/") or norm.startswith(m + "/")')
    _flips(_allow, m, f"find {MR}/sub -name x")


def test_m5_fuser_recursive():
    m = _mutant('["find", "rg", "fd", "du"]', '["find", "rg", "fd", "du", "fuser"]')
    _flips(_allow, m, f"fuser -m {MR}")


def test_m6_no_segmentation():
    m = _mutant("t == _END or _is_separator(t)", "t == _END")
    _flips(_allow, m, f"du -sh sub; ls {MR}")


def test_m7_no_relative_candidates():
    m = _mutant("return os.path.exists(os.path.join(cwd, tok))", "return False")
    _flips(_allow, m, "grep -r foo robot", MR)


def test_m8_no_cd_tracking():
    m = _mutant("seg_cwd = _after_cd(cur, seg_cwd)", "pass")
    _flips(_deny, m, f"cd {MR} && find .")


def test_m9_no_pattern_position_exclusion():
    m = _mutant("if cmd in _PATTERN_FIRST and not pattern_given:", "if False:")
    _flips(_deny, m, "rg src", MR)


def test_m10_cwd_fallback_always():
    m = _mutant("return roots if roots else [_resolve(cwd, cwd)]",
                "return roots + [_resolve(cwd, cwd)]")
    _flips(_allow, m, "rg foo src/", MR)


def test_m11_fail_open_wrapper_removed(monkeypatch, capsys):
    m = _mutant("return _run()\n    except Exception:", "return _run()\n    except KeyError:")
    with pytest.raises(Exception):
        _run_main(m, monkeypatch, capsys, "[]")


def test_m12_option_values_not_consumed():
    m = _mutant("if _takes_value(cmd, tok):", "if False:")
    _flips(_deny, m, "rg -g '*.py' robot", MR)


def test_m13_redirections_not_stripped():
    m = _mutant("_strip_redirects(cur)", "cur")
    _flips(_deny, m, "du -sh 2>/dev/null", MR)
