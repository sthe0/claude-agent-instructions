"""hook-scope-track.py: PostToolUse heartbeat + touched-path accumulation into the
session_scope registry. Driven end-to-end via subprocess with HOME pointed at a tmp
tree so DEFAULT_SCOPES_DIR (~/.claude/agentctl/scopes) resolves under tmp_path, and
PATH pointed at stub git/arc binaries (mirrors tests/test_detect_backend.py's stub
technique) so VCS detection is deterministic and offline.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from session_scope import registry

HOOK = Path(__file__).resolve().parent.parent / "hook-scope-track.py"
INSTALLER = Path(__file__).resolve().parent.parent / "install-reminder-hooks.sh"

_SPEC = importlib.util.spec_from_file_location("hook_scope_track", str(HOOK))
track_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(track_mod)


def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def _make_git_stub(bin_dir: Path, toplevel: Path, counter: "Path | None" = None) -> None:
    bump = f'echo x >> "{counter}"' if counter is not None else ":"
    _write_exec(
        bin_dir / "git",
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            case "$1 $2" in
              "rev-parse --show-toplevel") {bump}; printf '%s\\n' "{toplevel}" ;;
              *) exit 1 ;;
            esac
            """
        ),
    )


def _make_arc_stub(bin_dir: Path, root: Path) -> None:
    _write_exec(
        bin_dir / "arc",
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            case "$1" in
              "root") printf '%s\\n' "{root}" ;;
              *) exit 1 ;;
            esac
            """
        ),
    )


def run_hook(payload: dict, home: Path, bin_dir: str, lineage: "str | None" = None) -> subprocess.CompletedProcess:
    # bin_dir first so a stub shadows the real binary; /usr/bin:/bin kept so the
    # stub's own "#!/usr/bin/env bash" shebang can still resolve bash.
    env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"}
    if lineage is not None:
        env["AGENT_LINEAGE_IDS"] = lineage
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def edit_payload(session_id: str, cwd: str, file_path: str) -> dict:
    return {
        "tool_name": "Edit",
        "session_id": session_id,
        "cwd": cwd,
        "tool_input": {"file_path": file_path},
    }


def bash_payload(session_id: str, cwd: str, command: str = "ls") -> dict:
    return {
        "tool_name": "Bash",
        "session_id": session_id,
        "cwd": cwd,
        "tool_input": {"command": command},
    }


def _record(home: Path, session_id: str):
    scopes_dir = home / ".claude" / "agentctl" / "scopes"
    return registry.load(scopes_dir, session_id)


def test_edit_records_touch_and_context(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_stub(bin_dir, repo)
    target = repo / "a.py"
    target.write_text("x")

    proc = run_hook(edit_payload("s1", str(repo), str(target)), home, str(bin_dir))
    assert proc.returncode == 0

    rec = _record(home, "s1")
    assert rec is not None
    assert rec.touched_paths == [str(target.resolve())]
    assert rec.cwd == str(repo)
    assert rec.repo_root == str(repo)
    assert rec.vcs == "git"
    assert rec.heartbeat_ts > 0


def test_bash_updates_heartbeat_and_cwd_without_recording_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_stub(bin_dir, repo)

    proc = run_hook(
        bash_payload("s1", str(repo), "cat /etc/hostname"), home, str(bin_dir)
    )
    assert proc.returncode == 0

    rec = _record(home, "s1")
    assert rec is not None
    assert rec.touched_paths == []
    assert rec.cwd == str(repo)
    assert rec.vcs == "git"
    assert rec.heartbeat_ts > 0


def test_memory_path_not_recorded_but_heartbeat_still_updates(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_stub(bin_dir, repo)
    mem_dir = repo / "memory-global" / "leaves"
    mem_dir.mkdir(parents=True)
    mem_file = mem_dir / "note.md"
    mem_file.write_text("x")

    proc = run_hook(edit_payload("s1", str(repo), str(mem_file)), home, str(bin_dir))
    assert proc.returncode == 0

    rec = _record(home, "s1")
    assert rec is not None
    assert rec.touched_paths == []
    assert rec.heartbeat_ts > 0


def test_tmp_path_not_recorded(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_stub(bin_dir, repo)
    scratch = repo / "tmp" / "scratch.py"
    scratch.parent.mkdir(parents=True)
    scratch.write_text("x")

    proc = run_hook(edit_payload("s1", str(repo), str(scratch)), home, str(bin_dir))
    assert proc.returncode == 0

    rec = _record(home, "s1")
    assert rec is not None
    assert rec.touched_paths == []


def test_arc_used_when_git_absent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mount = tmp_path / "arcmount"
    mount.mkdir()
    _make_arc_stub(bin_dir, mount)

    proc = run_hook(bash_payload("s1", str(mount)), home, str(bin_dir))
    assert proc.returncode == 0

    rec = _record(home, "s1")
    assert rec is not None
    assert rec.vcs == "arc"
    assert rec.repo_root == str(mount)


def test_vcs_none_when_neither_git_nor_arc_available(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()  # empty — no git, no arc on PATH
    plain = tmp_path / "plainsubpath"
    plain.mkdir()

    proc = run_hook(bash_payload("s1", str(plain)), home, str(bin_dir))
    assert proc.returncode == 0

    rec = _record(home, "s1")
    assert rec is not None
    assert rec.vcs == "none"
    assert rec.repo_root is None


def test_repo_root_resolution_cached_when_cwd_unchanged(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    counter = tmp_path / "git-calls"
    _make_git_stub(bin_dir, repo, counter=counter)

    run_hook(bash_payload("s1", str(repo)), home, str(bin_dir))
    run_hook(bash_payload("s1", str(repo)), home, str(bin_dir))
    run_hook(bash_payload("s1", str(repo)), home, str(bin_dir))

    calls = counter.read_text().count("x") if counter.exists() else 0
    assert calls == 1


def test_never_blocks_on_malformed_stdin(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_never_emits_permission_decision(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_stub(bin_dir, repo)
    target = repo / "a.py"
    target.write_text("x")

    proc = run_hook(edit_payload("s1", str(repo), str(target)), home, str(bin_dir))
    assert proc.returncode == 0
    assert "permissionDecision" not in proc.stdout


def test_parse_etime_formats():
    assert track_mod.parse_etime("05") == 5.0
    assert track_mod.parse_etime("07:44") == 7 * 60 + 44
    assert track_mod.parse_etime("01:02:03") == 3600 + 2 * 60 + 3
    assert track_mod.parse_etime("2-01:02:03") == 2 * 86400 + 3600 + 2 * 60 + 3
    assert track_mod.parse_etime("  07:44 \n") == 7 * 60 + 44
    assert track_mod.parse_etime("") is None
    assert track_mod.parse_etime("garbage") is None
    assert track_mod.parse_etime("x-01:02") is None
    assert track_mod.parse_etime("1:2:3:4") is None


def _patch_ancestry(monkeypatch, my_pid: int, parents: dict, ages: dict) -> None:
    monkeypatch.setattr(track_mod.os, "getpid", lambda: my_pid)
    monkeypatch.setattr(track_mod, "_ppid_of", lambda pid: parents.get(pid))
    monkeypatch.setattr(track_mod, "_elapsed_s", lambda pid: ages.get(pid))


def test_session_pid_picks_first_measurably_older_ancestor(monkeypatch):
    # hook(100, 1s) <- wrapper shell(90, 1s, same age) <- session(80, 3600s)
    _patch_ancestry(
        monkeypatch,
        my_pid=100,
        parents={100: 90, 90: 80, 80: 1},
        ages={100: 1.0, 90: 1.0, 80: 3600.0},
    )
    assert track_mod.session_pid() == 80


def test_session_pid_none_when_no_ancestor_qualifies(monkeypatch):
    # every ancestor is as young as the hook itself, chain ends at init
    _patch_ancestry(
        monkeypatch,
        my_pid=100,
        parents={100: 90, 90: 1},
        ages={100: 1.0, 90: 1.5},
    )
    assert track_mod.session_pid() is None


def test_session_pid_none_when_own_age_unresolvable(monkeypatch):
    _patch_ancestry(monkeypatch, my_pid=100, parents={100: 90}, ages={90: 3600.0})
    assert track_mod.session_pid() is None


def test_session_pid_skips_unresolvable_ancestor_age(monkeypatch):
    # wrapper's age can't be read; the older grandparent still qualifies
    _patch_ancestry(
        monkeypatch,
        my_pid=100,
        parents={100: 90, 90: 80, 80: 1},
        ages={100: 1.0, 80: 3600.0},
    )
    assert track_mod.session_pid() == 80


def test_session_pid_respects_max_depth(monkeypatch):
    _patch_ancestry(
        monkeypatch,
        my_pid=100,
        parents={100: 99, 99: 98, 98: 97, 97: 80, 80: 1},
        ages={100: 1.0, 99: 1.0, 98: 1.0, 97: 1.0, 80: 3600.0},
    )
    assert track_mod.session_pid(max_depth=2) is None
    assert track_mod.session_pid(max_depth=4) == 80


def test_edit_persists_lineage_ids_from_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_stub(bin_dir, repo)
    target = repo / "a.py"
    target.write_text("x")

    proc = run_hook(
        edit_payload("child", str(repo), str(target)), home, str(bin_dir),
        lineage="parent,grandparent",
    )
    assert proc.returncode == 0

    rec = _record(home, "child")
    assert rec is not None
    assert rec.lineage_ids == ["parent", "grandparent"]
    assert rec.touched_paths == [str(target.resolve())]


def test_no_lineage_env_leaves_lineage_ids_empty(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_stub(bin_dir, repo)
    target = repo / "a.py"
    target.write_text("x")

    proc = run_hook(edit_payload("s1", str(repo), str(target)), home, str(bin_dir))
    assert proc.returncode == 0

    rec = _record(home, "s1")
    assert rec is not None
    assert rec.lineage_ids == []


def test_installer_registers_scope_track_for_edit_write_and_bash():
    text = INSTALLER.read_text(encoding="utf-8")
    assert '"PostToolUse",      "Edit|Write", "hook-scope-track.py"' in text
    assert '"PostToolUse",      "Bash",  "hook-scope-track.py"' in text
