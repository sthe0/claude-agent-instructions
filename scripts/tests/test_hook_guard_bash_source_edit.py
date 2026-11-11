"""Tests for hook-guard-bash-source-edit.py — deny a Bash write of literal text
(`sed -i`, `perl -i`, heredoc / here-string / echo / printf redirect, `| tee`)
into a git or arc work tree outside every allowlisted root.

Every test drives the hook as a subprocess through its stdin/stdout contract and
never imports it, so the stage's negative control can swap the file for a stub.

Deny-side fixtures (the git repo, the `.arc`-marker directory) and the plain
directory must lie outside every allowlisted scratch root, or every deny test
would pass vacuously. The module therefore names its own scratch root
($AGENTCTL_SCRATCH_ROOTS, one private directory), puts the fixtures in a sibling
directory, and runs the hook with the "/tmp/" path-segment exemption removed (see
HOOK_RUNNER): the verdict depends on neither where $HOME nor where /tmp is. The
fixture asserts that the fixtures are not exempt under those rules.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-guard-bash-source-edit.py"
sys.path.insert(0, str(SCRIPTS_DIR))
from agentctl import exempt_paths  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


TMP_SEGMENT = "/tmp/"

# The hook runs with the "/tmp/" path-segment exemption removed, so a fixture tree is
# judged by the scratch roots alone and no verdict depends on whether the checkout or
# pytest's tmp_path sits under /tmp. The scratch-root rule itself is exercised by
# test_allow_scratch_root_git_tree.
HOOK_RUNNER = (
    "import runpy, sys\n"
    "from agentctl import exempt_paths\n"
    f"exempt_paths._EXEMPT_SUBSTRINGS = tuple(s for s in exempt_paths._EXEMPT_SUBSTRINGS if s != {TMP_SEGMENT!r})\n"
    "runpy.run_path(sys.argv[1], run_name='__main__')\n"
)

_scratch_root: "Path | None" = None


def _hook_env() -> dict:
    env = {**os.environ, **GIT_ENV}
    env[exempt_paths.SCRATCH_ROOTS_ENV] = str(_scratch_root)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SCRIPTS_DIR), env.get("PYTHONPATH")]))
    for var in ("GIT_DIR", "GIT_WORK_TREE"):
        env.pop(var, None)
    return env


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), env=_hook_env(), check=True,
                   capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git("init", "--quiet", "-b", "main", ".", cwd=repo)
    (repo / "tracked.py").write_text("a = 1\n")
    (repo / "other.py").write_text("b = 2\n")
    (repo / "agent-memory").mkdir()
    (repo / "agent-memory" / "note.md").write_text("note\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "seed", cwd=repo)


def _in_scratch(path: Path) -> bool:
    p = os.path.realpath(path)
    r = os.path.realpath(_scratch_root)
    return p == r or p.startswith(r + os.sep)


def _hook_exempts(path: Path) -> bool:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(exempt_paths.SCRATCH_ROOTS_ENV, str(_scratch_root))
        patch.setattr(exempt_paths, "_EXEMPT_SUBSTRINGS",
                      tuple(s for s in exempt_paths._EXEMPT_SUBSTRINGS if s != TMP_SEGMENT))
        return exempt_paths.is_engine_exempt(str(path))


class Sandbox:
    def __init__(self, root: Path):
        self.root = root
        self.repo = root / "repo"
        self.arc = root / "arc"
        self.plain = root / "plain"


@pytest.fixture(scope="module", autouse=True)
def scratch_root(tmp_path_factory):
    global _scratch_root
    _scratch_root = tmp_path_factory.mktemp("hook-scratch-root")
    yield _scratch_root
    _scratch_root = None


@pytest.fixture(scope="module")
def sandbox(scratch_root, tmp_path_factory):
    root = tmp_path_factory.mktemp("hook-deny-fixtures")
    box = Sandbox(root)
    _init_repo(box.repo)
    (box.arc / ".arc").mkdir(parents=True)
    (box.arc / "src.py").write_text("a = 1\n")
    box.plain.mkdir()
    (box.plain / "f.txt").write_text("x\n")
    (root / "memory-global").symlink_to(box.repo)
    (root / "proj").symlink_to(box.repo / "agent-memory")
    (root / "link.py").symlink_to(box.repo / "tracked.py")
    assert not _in_scratch(root), f"{root} lies under a scratch root; deny tests would be vacuous"
    assert not _hook_exempts(root), f"{root} is engine-exempt"
    return box


def run_hook(command: str, cwd: "Path | str", tool: str = "Bash",
             env: "dict | None" = None) -> subprocess.CompletedProcess:
    payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}
    return run_raw(json.dumps(payload), env)


def run_raw(stdin: str, env: "dict | None" = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", HOOK_RUNNER, str(HOOK_SCRIPT)], input=stdin,
                          env=env or _hook_env(), capture_output=True, text=True)


def denied(proc: subprocess.CompletedProcess) -> bool:
    return proc.returncode == 0 and '"permissionDecision": "deny"' in proc.stdout


def allowed(proc: subprocess.CompletedProcess) -> bool:
    return proc.returncode == 0 and proc.stdout.strip() == ""


def assert_denied(command: str, cwd) -> None:
    proc = run_hook(command, cwd)
    assert denied(proc), f"expected deny for {command!r}; rc={proc.returncode} out={proc.stdout!r}"


def assert_allowed(command: str, cwd) -> None:
    proc = run_hook(command, cwd)
    assert allowed(proc), f"expected allow for {command!r}; rc={proc.returncode} out={proc.stdout!r}"


# --- DENY: a literal-text write shape aimed at a tracked tree ---

def test_deny_sed_i_git_tree(sandbox):
    assert_denied("sed -i s/a/b/ tracked.py", sandbox.repo)
    assert_denied("sed -i.bak -e 's/a/b/' tracked.py", sandbox.repo)
    assert_denied(f"sed -n -i s/a/b/ {sandbox.repo}/tracked.py", sandbox.plain)


def test_deny_perl_i_git_tree(sandbox):
    assert_denied("perl -i -pe 's/a/b/' tracked.py", sandbox.repo)
    assert_denied("perl -pi -e 's/a/b/' tracked.py", sandbox.repo)
    assert_denied("perl -i.bak -pe 's/a/b/' tracked.py", sandbox.repo)


def test_deny_heredoc_git_tree(sandbox):
    assert_denied("cat > tracked.py <<EOF\nx = 2\nEOF", sandbox.repo)
    assert_denied("cat >> tracked.py <<'EOF'\nx = 2\nEOF", sandbox.repo)


def test_deny_herestring_git_tree(sandbox):
    assert_denied("cat <<< text > tracked.py", sandbox.repo)


def test_deny_tee_git_tree(sandbox):
    assert_denied("echo text | tee tracked.py", sandbox.repo)
    assert_denied("echo text | tee -a tracked.py", sandbox.repo)
    assert_denied("cat <<EOF | tee tracked.py\nx = 2\nEOF", sandbox.repo)


def test_deny_printf_git_tree(sandbox):
    assert_denied("printf 'x = 2\\n' > tracked.py", sandbox.repo)


def test_deny_echo_git_tree(sandbox):
    assert_denied("echo text > tracked.py", sandbox.repo)
    assert_denied("echo text >> tracked.py", sandbox.repo)
    assert_denied(f"echo text > {sandbox.repo}/tracked.py", sandbox.plain)


def test_deny_printf_append_git_tree(sandbox):
    assert_denied("printf 'x' >> tracked.py", sandbox.repo)


def test_deny_echo_chained_git_tree(sandbox):
    assert_denied("true && echo text > tracked.py", sandbox.repo)
    assert_denied("echo ok; echo text > tracked.py", sandbox.repo)
    assert_denied('echo "> a quote" && echo text > tracked.py', sandbox.repo)


def test_deny_echo_fd_dup_git_tree(sandbox):
    assert_denied("echo text 2>&1 > tracked.py", sandbox.repo)


def test_deny_echo_untracked(sandbox):
    assert_denied("echo text > brand_new.py", sandbox.repo)


def test_deny_symlink_target(sandbox):
    assert_denied("echo text > link.py", sandbox.root)


def test_deny_cd_then_write(sandbox):
    assert_denied(f"cd {sandbox.repo} && echo text > tracked.py", sandbox.plain)


def test_deny_arc_tree(sandbox):
    assert_denied("sed -i s/a/b/ src.py", sandbox.arc)
    assert_denied("echo text > src.py", sandbox.arc)


def test_reason_names_edit(sandbox):
    proc = run_hook("echo text > tracked.py", sandbox.repo)
    assert denied(proc), proc.stdout
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "hook-guard-bash-source-edit.py" in reason
    assert "Edit/Write" in reason


def test_payload_invariant_heredoc_body(sandbox):
    code_like = "cat > tracked.py <<EOF\nimport os\nprint(os.getcwd())\nEOF"
    prose_like = "cat > tracked.py <<EOF\nThis note says it's fine; nothing here is code.\nEOF"
    assert_denied(code_like, sandbox.repo)
    assert_denied(prose_like, sandbox.repo)


# --- ALLOW: allowlisted roots, non-write shapes, unreadable shapes ---

def test_allow_tmp_and_scratch(sandbox):
    assert_allowed("echo text > /tmp/hook-guard-bash-source-edit-probe.txt", sandbox.repo)
    assert_allowed("printf x | tee /tmp/hook-guard-bash-source-edit-probe.txt", sandbox.repo)
    assert_allowed("sed -i s/a/b/ /tmp/hook-guard-bash-source-edit-probe.txt", sandbox.repo)
    assert_allowed("perl -i -pe 's/a/b/' /tmp/hook-guard-bash-source-edit-probe.txt", sandbox.repo)


def test_allow_memory_root(sandbox):
    assert_allowed(f"echo text > {sandbox.repo}/memory-global/leaves/n.md", sandbox.repo)
    assert_allowed(f"echo text > {sandbox.repo}/agent-memory/note.md", sandbox.repo)
    assert_allowed("sed -i s/a/b/ agent-memory/note.md", sandbox.repo)


def test_allow_memory_symlink(sandbox):
    assert_allowed("echo text > memory-global/tracked.py", sandbox.root)
    assert_allowed("echo text > proj/note.md", sandbox.root)


def test_allow_generator_redirect(sandbox):
    assert_allowed("python3 gen.py > out.py", sandbox.repo)
    assert_allowed("git diff > out.patch", sandbox.repo)
    assert_allowed("echo seed | python3 gen.py > out.py", sandbox.repo)
    assert_allowed("sed s/a/b/ tracked.py > /tmp/hook-guard-bash-source-edit-probe.txt", sandbox.repo)


def test_allow_cp_mv_git(sandbox):
    assert_allowed("cp other.py tracked.py", sandbox.repo)
    assert_allowed("mv other.py tracked.py", sandbox.repo)
    assert_allowed("git add -A", sandbox.repo)
    assert_allowed("git commit -m seed", sandbox.repo)


def test_allow_cat_copy(sandbox):
    assert_allowed("cat other.py > tracked.py", sandbox.repo)
    assert_allowed("cat < other.py > tracked.py", sandbox.repo)
    assert_allowed("cat other.py | tee tracked.py", sandbox.repo)
    assert_allowed("python3 gen.py | cat > tracked.py", sandbox.repo)


def test_allow_plain_dir(sandbox):
    assert_allowed("echo text > f.txt", sandbox.plain)
    assert_allowed("sed -i s/x/y/ f.txt", sandbox.plain)


def test_allow_home_arc_state_dir(sandbox):
    fake_home = sandbox.root / "fake-home"
    (fake_home / ".arc" / "mount-points").mkdir(parents=True)
    (fake_home / "notes").mkdir()
    (fake_home / "mount" / ".arc").mkdir(parents=True)
    env = {**_hook_env(), "HOME": str(fake_home)}

    assert allowed(run_hook("echo text > f.txt", fake_home / "notes", env=env))
    assert denied(run_hook("echo text > f.txt", fake_home / "mount", env=env))


def test_allow_scratch_root_git_tree(scratch_root):
    assert _in_scratch(scratch_root), f"{scratch_root} is not under a scratch root"
    repo = scratch_root / "repo"
    _init_repo(repo)
    assert_allowed("sed -i s/a/b/ tracked.py", repo)
    assert_allowed("echo text > tracked.py", repo)


def test_allow_unreadable_target_shapes(sandbox):
    assert_allowed("echo text > $SOMEVAR/f.py", sandbox.repo)
    assert_allowed("echo text > $(mktemp)", sandbox.repo)
    assert_allowed("diff <(echo a) <(echo b) | tee /tmp/hook-guard-bash-source-edit-probe.txt", sandbox.repo)


def test_allow_quoted_redirect_lookalike(sandbox):
    assert_allowed('echo "> a quote" > /tmp/probe', sandbox.repo)
    assert_allowed("printf '> %s' x | tee /tmp/x", sandbox.repo)


def test_allow_other_tools_and_empty(sandbox):
    assert allowed(run_hook("echo text > tracked.py", sandbox.repo, tool="Edit"))
    assert_allowed("", sandbox.repo)


def test_payload_invariant_quoted_mention(sandbox):
    assert_allowed("git commit -m 'fix sed -i usage'", sandbox.repo)
    assert_allowed('git commit -m "fix sed -i usage"', sandbox.repo)
    assert_allowed("git commit -m 'fix sed -i tracked.py'", sandbox.repo)
    assert_allowed('git commit -m "fix sed -i tracked.py"', sandbox.repo)
    assert_allowed("echo 'fix sed -i usage' > /tmp/hook-guard-bash-source-edit-probe.txt", sandbox.repo)
    assert_allowed('echo "fix sed -i usage" > /tmp/hook-guard-bash-source-edit-probe.txt', sandbox.repo)


# --- FAIL OPEN: nothing this hook cannot read may block a call ---

def test_fail_open_garbage_stdin(sandbox):
    for garbage in ("not json {", "", "[1, 2]", "null"):
        proc = run_raw(garbage)
        assert allowed(proc), f"garbage {garbage!r}: rc={proc.returncode} out={proc.stdout!r}"


def test_fail_open_shell_unparsable(sandbox):
    assert_allowed("echo 'unbalanced > tracked.py", sandbox.repo)
    assert_allowed("cat > tracked.py <<EOF\nno terminator line", sandbox.repo)
