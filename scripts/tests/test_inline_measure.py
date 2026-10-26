"""scripts/inline-measure.py on a throwaway git repository with its own .gitattributes."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentctl.config import Thresholds

SCRIPT = Path(__file__).resolve().parent.parent / "inline-measure.py"
BOUND = Thresholds().small_change_max_lines


def _git(repo, *argv):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *argv],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _write(repo, rel, lines):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line {i}\n" for i in range(lines)), encoding="utf-8")


def _run(repo, *argv):
    return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *argv], capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitattributes").write_text("gen/out.toml linguist-generated=true\n", encoding="utf-8")
    _write(tmp_path, "src/a.py", 1)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _commit(repo, **files):
    for rel, lines in files.items():
        _write(repo, rel.replace("__", "/"), lines)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD~1"), _git(repo, "rev-parse", "HEAD")


def test_json_counts_only_hand_written_production_lines(repo):
    base, tip = _commit(repo, **{"src__a.py": 6, "gen__out.toml": 300, "scripts__tests__test_a.py": 50})
    done = _run(repo, base, tip)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report["production_lines"] == 5
    assert report["bound"] == BOUND
    assert report["within_bound"] is True
    assert report["counted_paths"] == ["src/a.py"]
    assert set(report["excluded"]) == {"generated", "test"}


def test_exit_one_over_the_bound(repo):
    base, tip = _commit(repo, **{"src__a.py": 600})
    done = _run(repo, base, tip)
    assert done.returncode == 1
    assert json.loads(done.stdout)["within_bound"] is False


def test_lines_prints_only_the_integer(repo):
    base, tip = _commit(repo, **{"src__a.py": 600})
    done = _run(repo, "--lines", base, tip)
    assert done.returncode == 0
    assert done.stdout.strip() == "599"


def test_an_unknown_revision_exits_two(repo):
    done = _run(repo, "no-such-rev", "HEAD")
    assert done.returncode == 2
    assert done.stdout == ""
