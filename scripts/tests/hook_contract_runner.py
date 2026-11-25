"""Runs a hook script on a recorded Claude payload in a throw-away world and reduces its answer to a verdict.

Shared by test_hook_unparsable_input_contract.py and by the recording of
fixtures/claude_hook_payloads/expected_verdicts.json. A fixture is a JSON file
``{"hook": <script basename>, "files": {<path under the world>: <text>}, "payload": {...}}``; the strings
``{root}`` and ``{core}`` in the payload are replaced by the world's directory and by the git repository
the canon guard treats as the serving checkout.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import io
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_hook_payloads"
EXPECTED = FIXTURES / "expected_verdicts.json"
KINDS = ("claude", "unreadable")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
}


def fixture_names() -> list[str]:
    return sorted(f"{kind}/{p.name}" for kind in KINDS for p in (FIXTURES / kind).glob("*.json"))


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_world(base: Path) -> tuple[Path, Path]:
    """(root, core): an empty directory and, inside it, a git repository on ``main`` holding scripts/existing.py."""
    root = base / "world"
    core = root / "core"
    (core / "scripts").mkdir(parents=True)
    (core / "scripts" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    env = {**os.environ, **_GIT_ENV}
    for args in (["init", "--quiet", "-b", "main", "."], ["add", "-A"], ["commit", "--quiet", "-m", "seed"]):
        subprocess.run(["git", *args], cwd=core, env=env, check=True, capture_output=True)
    return root, core


def _filled(value, root: Path, core: Path):
    if isinstance(value, str):
        return value.replace("{root}", str(root)).replace("{core}", str(core))
    if isinstance(value, list):
        return [_filled(v, root, core) for v in value]
    if isinstance(value, dict):
        return {k: _filled(v, root, core) for k, v in value.items()}
    return value


def verdict_of(returncode: int, stdout: str) -> str:
    """allow (no answer), or the permissionDecision the hook printed; crash for a non-zero exit."""
    if returncode != 0:
        return f"crash:{returncode}"
    if not stdout.strip():
        return "allow"
    answer = json.loads(stdout).get("hookSpecificOutput") or {}
    return answer.get("permissionDecision", "allow")


def run_fixture(scripts_dir: Path, name: str, base: Path) -> str:
    """The verdict of ``scripts_dir``'s hook on fixture ``name``, in a fresh world under ``base``."""
    spec = load_fixture(name)
    root, core = make_world(base)
    for rel, text in (spec.get("files") or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    env = {
        **os.environ, **_GIT_ENV,
        "HOME": str(root), "CLAUDE_AGENT_HOME": str(root / ".claude-agent"),
        "CLAUDE_INSTRUCTIONS_REPO": str(core), "CLAUDE_CANON_ROOTS_FILE": str(root / "no-canon-roots"),
        "CLAUDE_PERMISSION_GUARD_LOG": str(root / "guard.jsonl"), "AGENT_RECURSION_DEPTH": "0",
    }
    proc = subprocess.run(
        [sys.executable, str(scripts_dir / spec["hook"])],
        input=json.dumps(_filled(spec["payload"], root, core)), env=env, capture_output=True, text=True,
        timeout=60,
    )
    return verdict_of(proc.returncode, proc.stdout)


def verdicts(scripts_dir: Path, base: Path) -> dict[str, str]:
    out = {}
    for name in fixture_names():
        out[name] = run_fixture(scripts_dir, name, base / name.replace("/", "_").removesuffix(".json"))
    return out


def scripts_at_ref(ref: str, dest: Path) -> Path:
    """``scripts/`` of ``ref`` (tests left out) extracted under ``dest``; returns the extracted scripts directory."""
    archive = subprocess.run(
        ["git", "-C", str(REPO), "archive", "--format=tar", ref, "--", "scripts", ":(exclude)scripts/tests"],
        capture_output=True, check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    return dest / "scripts"
