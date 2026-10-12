"""memory-leaf-size-calibrate.py: tolerates a non-UTF-8 leaf, rejects a non-commit rev, is reproducible."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "memory-leaf-size-calibrate.py"
spec = importlib.util.spec_from_file_location("memory_leaf_size_calibrate", SCRIPT)
cal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cal)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _leaf(i: int) -> bytes:
    body = "".join(f"### c{j}\n\nt\n\n" for j in range(i + 1))
    return f"---\nschema: difficulty/v1\n---\n\n## Contexts\n\n{body}".encode()


@pytest.fixture
def repo(tmp_path):
    leaves = tmp_path / "memory-global" / "leaves"
    leaves.mkdir(parents=True)
    for i in range(6):
        (leaves / f"l{i}.md").write_bytes(_leaf(i))
    (leaves / "bad.md").write_bytes(b"---\nschema: difficulty/v1\n---\n\xff\xfe\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    return tmp_path


def test_non_utf8_leaf_is_skipped_not_fatal(repo):
    result = cal.calibrate(repo, "HEAD")
    assert result["n_leaves"] == 6


def test_non_commit_rev_is_a_clear_exit(repo):
    with pytest.raises(SystemExit, match="not a commit"):
        cal.calibrate(repo, "no-such-rev")


def test_output_is_reproducible_and_dated_by_the_commit(repo):
    first, second = cal.calibrate(repo, "HEAD"), cal.calibrate(repo, "HEAD")
    assert first == second
    assert first["measured_on"] == subprocess.run(
        ["git", "-C", str(repo), "show", "-s", "--format=%cs", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
