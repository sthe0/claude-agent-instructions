"""Installer test for apply-cursor-config.sh merge semantics.

Runs the real script via subprocess with CURSOR_CLI_CONFIG / CURSOR_CLI_BASE
redirected under tmp_path so the live ~/.cursor/cli-config.json is never
touched. Skips the permissions symlink via SKIP_CURSOR_PERMISSIONS_LINK=1.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APPLY = REPO / "cursor" / "scripts" / "apply-cursor-config.sh"
REPO_BASE = REPO / "cursor" / "config" / "cli-base.json"


@pytest.fixture(autouse=True)
def _require_tools():
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq required for apply-cursor-config.sh")


def _run(
    *,
    target: Path,
    base: Path,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "CURSOR_CLI_CONFIG": str(target),
        "CURSOR_CLI_BASE": str(base),
        "SKIP_CURSOR_PERMISSIONS_LINK": "1",
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(APPLY)],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def base_file(tmp_path: Path) -> Path:
    path = tmp_path / "cli-base.json"
    path.write_text(
        json.dumps(
            {
                "approvalMode": "auto-review",
                "permissions": {
                    "allow": ["Shell(ls)", "Shell(git)"],
                    "deny": ["Shell(sudo)", "Shell(su)"],
                },
                "sandbox": {
                    "mode": "disabled",
                    "networkAccess": "user_config_with_defaults",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def target_file(tmp_path: Path) -> Path:
    path = tmp_path / "cli-config.json"
    path.write_text(
        json.dumps(
            {
                "approvalMode": "allowlist",
                "permissions": {
                    "allow": ["Shell(git)", "Shell(local-only)"],
                    "deny": ["Shell(dd)"],
                },
                "sandbox": {
                    "mode": "enabled",
                    "networkAccess": "restricted",
                    "extraLocalKey": "keep",
                },
                "authInfo": {"email": "keep@example.com", "userId": 1},
                "model": {"modelId": "local-model"},
            }
        ),
        encoding="utf-8",
    )
    yield path
    bak = Path(str(path) + ".bak")
    if bak.exists():
        bak.unlink()


def test_merge_approval_allow_deny_auth(base_file: Path, target_file: Path):
    result = _run(target=target_file, base=base_file)
    assert result.returncode == 0, result.stderr or result.stdout

    data = json.loads(target_file.read_text(encoding="utf-8"))
    assert data["approvalMode"] == "auto-review"
    assert data["permissions"]["allow"] == [
        "Shell(ls)",
        "Shell(git)",
        "Shell(local-only)",
    ]
    assert "Shell(sudo)" in data["permissions"]["deny"]
    assert "Shell(su)" in data["permissions"]["deny"]
    assert "Shell(dd)" in data["permissions"]["deny"]
    assert data["authInfo"] == {"email": "keep@example.com", "userId": 1}
    assert data["model"] == {"modelId": "local-model"}
    assert data["sandbox"]["mode"] == "disabled"
    assert data["sandbox"]["networkAccess"] == "user_config_with_defaults"
    assert data["sandbox"]["extraLocalKey"] == "keep"


def test_idempotent(base_file: Path, target_file: Path):
    assert _run(target=target_file, base=base_file).returncode == 0
    after_first = json.loads(target_file.read_text(encoding="utf-8"))
    assert _run(target=target_file, base=base_file).returncode == 0
    after_second = json.loads(target_file.read_text(encoding="utf-8"))
    assert after_first == after_second


def test_missing_target_creates_empty_then_base(tmp_path: Path, base_file: Path):
    target = tmp_path / "subdir" / "cli-config.json"
    result = _run(target=target, base=base_file)
    assert result.returncode == 0, result.stderr or result.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["approvalMode"] == "auto-review"
    assert data["permissions"]["allow"] == ["Shell(ls)", "Shell(git)"]


def test_repo_base_file_loads(tmp_path: Path):
    """Smoke: real repo cli-base.json is valid and merges into a minimal local."""
    assert REPO_BASE.is_file()
    target = tmp_path / "cli-config.json"
    target.write_text(
        json.dumps({"authInfo": {"email": "x@y.z"}, "permissions": {"allow": []}}),
        encoding="utf-8",
    )
    result = _run(target=target, base=REPO_BASE)
    assert result.returncode == 0, result.stderr or result.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["approvalMode"] == "auto-review"
    assert "Shell(sudo)" in data["permissions"]["deny"]
    assert data["authInfo"] == {"email": "x@y.z"}
    assert len(data["permissions"]["allow"]) >= 1


def test_permissions_symlink_refuse_regular_file(tmp_path: Path, base_file: Path):
    target = tmp_path / "cli-config.json"
    target.write_text("{}", encoding="utf-8")
    src = tmp_path / "permissions.json"
    src.write_text('{"autoRun": {}}', encoding="utf-8")
    dst = tmp_path / "home-permissions.json"
    dst.write_text("regular", encoding="utf-8")
    result = _run(
        target=target,
        base=base_file,
        env_extra={
            "SKIP_CURSOR_PERMISSIONS_LINK": "0",
            "CURSOR_PERMISSIONS_SRC": str(src),
            "CURSOR_PERMISSIONS_DST": str(dst),
        },
    )
    assert result.returncode != 0
    assert "refuse:" in (result.stderr or "")
