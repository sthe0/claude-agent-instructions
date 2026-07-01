"""backends/arc.sh: arc-mount workspace backend (Component C, Stage 6).

Driven via subprocess with an ARC_BIN stub, exactly as test_session_isolate.py
stubs GIT_BIN: the stub logs every invocation, answers `mount --list --json`
from a fixture JSON file, and never really creates a mount (`mount -m`) or
checks out a branch (`checkout -b`) — it only records that it was asked to. No
real arc mount is ever created, so the suite stays hermetic in CI.

CLAUDE_DRY_RUN is the seam backends/arc.sh honors: under dry-run,
backend_ensure_workspace makes zero mount-creating calls and creates no
directory, yet still reports the would-be mount path — the "zero mutation" half
of Stage 6's done criterion. A second test group drives session-isolate.sh with
the backend NAME resolved to `arc` to prove the router dispatches to this
backend with no router change (backend-blind name resolution).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_scope import registry  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ARC_SH = REPO / "project_entry" / "backends" / "arc.sh"
ISOLATE_SH = REPO / "session-isolate.sh"


def _stub_arc(tmp_path: Path, calls_log: Path, mounts_json: Path) -> Path:
    """An ARC_BIN stub matching test_session_isolate.py's GIT_BIN-stub shape:
    logs every invocation, answers `mount --list` from mounts_json, and treats
    `mount -m` / `checkout -b` as recorded no-ops (never touching a real repo)."""
    stub = tmp_path / "arc-stub"
    stub.write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >>{calls_log}
case "$1 $2" in
  "mount --list")  cat {mounts_json} ;;
  "mount -m")      : ;;
  "checkout -b")   : ;;
  *) : ;;
esac
""")
    stub.chmod(0o755)
    return stub


def _write_mounts(mounts_json: Path, entries: "list[dict]") -> None:
    mounts_json.write_text(json.dumps(entries))


def _mounted(mount: str, object_store: str = "/store/objects") -> dict:
    return {"status": "mounted", "mount": mount, "object-store": object_store}


def _run_backend(
    tmp_path: Path,
    snippet: str,
    cwd: Path,
    calls_log: Path,
    mounts_json: Path,
    dry_run: bool = True,
    workspace_root: "str | None" = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ARC_BIN"] = str(_stub_arc(tmp_path, calls_log, mounts_json))
    if dry_run:
        env["CLAUDE_DRY_RUN"] = "1"
    else:
        env.pop("CLAUDE_DRY_RUN", None)
    if workspace_root is not None:
        env["CLAUDE_WORKSPACE_ROOT"] = workspace_root
    else:
        env.pop("CLAUDE_WORKSPACE_ROOT", None)
    return subprocess.run(
        ["bash", "-c", f'source "{ARC_SH}"; {snippet}'],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


# ── backend_detect ──────────────────────────────────────────────────────────


def test_detect_true_when_cwd_inside_mounted_mount(tmp_path):
    main = tmp_path / "arcadia"
    main.mkdir()
    calls = tmp_path / "calls.log"
    mounts = tmp_path / "mounts.json"
    calls.write_text("")
    _write_mounts(mounts, [_mounted(str(main))])

    proc = _run_backend(tmp_path, "backend_detect", main, calls, mounts)
    assert proc.returncode == 0, proc.stderr


def test_detect_false_when_cwd_outside_any_mount(tmp_path):
    main = tmp_path / "arcadia"
    main.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    calls = tmp_path / "calls.log"
    mounts = tmp_path / "mounts.json"
    calls.write_text("")
    _write_mounts(mounts, [_mounted(str(main))])

    proc = _run_backend(tmp_path, "backend_detect", outside, calls, mounts)
    assert proc.returncode != 0


def test_detect_false_when_mount_not_mounted(tmp_path):
    main = tmp_path / "arcadia"
    main.mkdir()
    calls = tmp_path / "calls.log"
    mounts = tmp_path / "mounts.json"
    calls.write_text("")
    _write_mounts(mounts, [{"status": "unmounted", "mount": str(main), "object-store": "/s"}])

    proc = _run_backend(tmp_path, "backend_detect", main, calls, mounts)
    assert proc.returncode != 0


# ── backend_ensure_workspace ────────────────────────────────────────────────


def test_ensure_dry_run_reports_mount_no_creation(tmp_path):
    main = tmp_path / "arcadia"
    main.mkdir()
    calls = tmp_path / "calls.log"
    mounts = tmp_path / "mounts.json"
    calls.write_text("")
    _write_mounts(mounts, [_mounted(str(main), "/store/objects")])

    proc = _run_backend(
        tmp_path, "backend_ensure_workspace task-name task-name", main, calls, mounts, dry_run=True
    )
    assert proc.returncode == 0, proc.stderr

    expected = str(main) + "_task-name"
    assert proc.stdout.strip().splitlines()[-1] == expected
    # Zero mutation: neither mount-creating call fired, and no dir was made.
    log = calls.read_text()
    assert "mount -m" not in log
    assert "checkout -b" not in log
    assert not Path(expected).exists()
    # The dry-run advisory names the object-store it WOULD share.
    assert "/store/objects" in proc.stderr


def test_ensure_non_dry_run_creates_mount_and_checks_out(tmp_path):
    main = tmp_path / "arcadia"
    main.mkdir()
    calls = tmp_path / "calls.log"
    mounts = tmp_path / "mounts.json"
    calls.write_text("")
    _write_mounts(mounts, [_mounted(str(main), "/store/objects")])

    proc = _run_backend(
        tmp_path, "backend_ensure_workspace task-name task-name", main, calls, mounts, dry_run=False
    )
    assert proc.returncode == 0, proc.stderr

    expected = str(main) + "_task-name"
    assert proc.stdout.strip().splitlines()[-1] == expected
    log = calls.read_text()
    assert f"mount -m {expected} --object-store /store/objects --override-object-store" in log
    assert "checkout -b task-name" in log
    assert Path(expected).is_dir()


def test_ensure_reuses_existing_mount(tmp_path):
    main = tmp_path / "arcadia"
    main.mkdir()
    expected = str(main) + "_task-name"
    calls = tmp_path / "calls.log"
    mounts = tmp_path / "mounts.json"
    calls.write_text("")
    _write_mounts(mounts, [_mounted(str(main)), _mounted(expected)])

    proc = _run_backend(
        tmp_path, "backend_ensure_workspace task-name task-name", main, calls, mounts, dry_run=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == expected
    log = calls.read_text()
    assert "mount -m" not in log
    assert "reusing existing mount" in proc.stderr


def test_ensure_fails_when_not_inside_a_mount(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    calls = tmp_path / "calls.log"
    mounts = tmp_path / "mounts.json"
    calls.write_text("")
    _write_mounts(mounts, [])

    proc = _run_backend(
        tmp_path, "backend_ensure_workspace task-name task-name", outside, calls, mounts
    )
    assert proc.returncode != 0
    assert "not inside a mounted arc mount" in proc.stderr


def test_compose_is_noop(tmp_path):
    main = tmp_path / "arcadia"
    main.mkdir()
    calls = tmp_path / "calls.log"
    mounts = tmp_path / "mounts.json"
    calls.write_text("")
    _write_mounts(mounts, [_mounted(str(main))])

    proc = _run_backend(tmp_path, "backend_compose /whatever", main, calls, mounts)
    assert proc.returncode == 0, proc.stderr


# ── session-isolate.sh dispatches to the arc backend (backend-blind) ─────────


def _scopes_dir(home: Path) -> Path:
    return home / ".claude" / "agentctl" / "scopes"


def test_session_isolate_dispatches_to_arc_backend(tmp_path):
    """With the backend NAME resolved to `arc`, session-isolate.sh sources
    backends/arc.sh and re-registers the session's scope at the new MOUNT root —
    proving the router is backend-blind (no git/arc branching in the router)."""
    home = tmp_path / "home"
    home.mkdir()
    main = tmp_path / "arcadia"
    main.mkdir()
    calls = tmp_path / "calls.log"
    mounts = tmp_path / "mounts.json"
    calls.write_text("")
    _write_mounts(mounts, [_mounted(str(main), "/store/objects")])

    detector = tmp_path / "det-arc.py"
    detector.write_text("print('arc startrek')\n")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["ARC_BIN"] = str(_stub_arc(tmp_path, calls, mounts))
    env["PATH"] = "/usr/bin:/bin"
    env["CLAUDE_SESSION_ID"] = "s-arc"
    env["CLAUDE_DRY_RUN"] = "1"
    env["CLAUDE_BACKEND_DETECTOR"] = str(detector)

    proc = subprocess.run(
        ["bash", str(ISOLATE_SH), "task-name"],
        cwd=str(main),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    expected = str(main) + "_task-name"
    assert proc.stdout.strip().splitlines()[-1] == expected
    assert "workspace=arc" in proc.stderr
    # No real mount created under dry-run.
    assert "mount -m" not in calls.read_text()

    rec = registry.load(_scopes_dir(home), "s-arc")
    assert rec is not None
    assert rec.repo_root == expected
    assert rec.vcs == "arc"
