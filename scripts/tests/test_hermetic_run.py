"""hermetic_run.py hides paths and the network from a command, in its own namespace only."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import hermetic_run

RUNNER = Path(hermetic_run.__file__).resolve()
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}

pytestmark = pytest.mark.skipif(bool(hermetic_run.check()), reason="user namespaces unavailable here")

LIST_DIR = "import os, sys; print(sorted(os.listdir(sys.argv[1])))"
LIST_ENV_DIR = "import os; print(sorted(os.listdir(os.environ['HIDDEN'])))"
CONNECT = """
import socket, sys
s = socket.socket()
s.settimeout(3)
try:
    s.connect((sys.argv[1], 80))
except OSError as exc:
    print("refused", exc.errno)
    raise SystemExit(0)
print("connected")
"""
LOOPBACK = """
import socket
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(1)
client = socket.socket()
client.connect(server.getsockname())
print("loopback ok")
"""


def hermetic(args: list[str], cmd: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RUNNER), *args, "--", *cmd], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=60)


def py(code: str, *argv: str) -> list[str]:
    return [sys.executable, "-c", code, *argv]


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "live"
    path.mkdir()
    (path / "refs").write_text("secret\n")
    return path


def test_a_hidden_directory_reads_as_empty(live):
    proc = hermetic(["--hide", str(live)], py(LIST_DIR, str(live)))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_the_same_directory_without_hiding_is_visible(live):
    assert hermetic([], py(LIST_DIR, str(live))).stdout.strip() == "['refs']"


def test_hiding_changes_nothing_outside_the_command(live):
    hermetic(["--hide", str(live)], py(LIST_DIR, str(live)))
    assert (live / "refs").read_text() == "secret\n"


def test_a_hidden_path_reached_through_a_symlink_made_outside_it_reads_as_empty(live, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(live)
    proc = hermetic(["--hide", str(live)], py(LIST_DIR, str(link)))
    assert proc.stdout.strip() == "[]"


def test_a_symlink_given_to_hide_hides_its_target(live, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(live)
    proc = hermetic(["--hide", str(link)], py(LIST_DIR, str(live)))
    assert proc.stdout.strip() == "[]"


def test_a_hidden_path_passed_in_an_environment_variable_reads_as_empty(live):
    proc = hermetic(["--hide", str(live)], py(LIST_ENV_DIR), env={"HIDDEN": str(live)})
    assert proc.stdout.strip() == "[]"


def test_a_relative_path_into_a_hidden_directory_reads_as_empty(live):
    inside = subprocess.run([sys.executable, str(RUNNER), "--hide", str(live), "--", *py("import os; print(os.listdir('.'))")],
                            cwd=live, capture_output=True, text=True, timeout=60)
    assert inside.stdout.strip() == "[]"


def test_a_hidden_file_reads_as_empty(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token\n")
    proc = hermetic(["--hide", str(secret)], py("import sys; print(repr(open(sys.argv[1]).read()))", str(secret)))
    assert proc.stdout.strip() == "''"


def test_a_hidden_directory_cannot_be_written(live):
    proc = hermetic(["--hide", str(live)], py("open(__import__('sys').argv[1] + '/new', 'w')", str(live)))
    assert proc.returncode != 0
    assert not (live / "new").exists()


def test_a_missing_path_is_skipped(tmp_path):
    proc = hermetic(["--hide", str(tmp_path / "absent")], py("print('ran')"))
    assert proc.stdout.strip() == "ran"


def test_a_connect_to_an_outside_address_fails():
    proc = hermetic([], py(CONNECT, "192.0.2.1"))
    assert proc.stdout.startswith("refused"), proc.stdout + proc.stderr


def test_loopback_works():
    proc = hermetic([], py(LOOPBACK))
    assert proc.stdout.strip() == "loopback ok", proc.stderr


def test_the_exit_status_is_the_commands():
    assert hermetic([], py("raise SystemExit(7)")).returncode == 7


def test_a_missing_command_is_exit_2():
    assert hermetic([], ["/nonexistent/command"]).returncode == 2


def _shared_clone(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    run = lambda *argv, cwd=None: subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True,  # noqa: E731
                                                  env={**os.environ, **GIT_ENV})
    run("init", "-q", str(source))
    (source / "f").write_text("1\n")
    run("add", "f", cwd=source)
    run("commit", "-qm", "first", cwd=source)
    run("clone", "-q", "--shared", "--no-checkout", str(source / ".git"), str(sandbox / "core"))
    (sandbox / "source-objects").mkdir()
    return source, sandbox / "core", sandbox / "source-objects"


def test_a_shared_clone_keeps_working_when_only_the_sources_objects_stay_visible(tmp_path):
    source, core, mount = _shared_clone(tmp_path)
    (core / ".git" / "objects" / "info" / "alternates").write_text(f"{mount}\n")
    proc = hermetic(["--hide", str(source), "--objects", f"{source / '.git' / 'objects'}:{mount}"],
                    ["git", "-C", str(core), "log", "--format=%s"], env=GIT_ENV)
    assert proc.stdout.strip() == "first", proc.stderr


def test_a_shared_clone_cannot_read_the_hidden_source_without_the_objects_rebinding(tmp_path):
    source, core, _ = _shared_clone(tmp_path)
    proc = hermetic(["--hide", str(source)], ["git", "-C", str(core), "log", "--format=%s"], env=GIT_ENV)
    assert proc.returncode != 0


def test_the_hidden_source_refs_are_not_readable_while_its_objects_are(tmp_path):
    source, core, mount = _shared_clone(tmp_path)
    (core / ".git" / "objects" / "info" / "alternates").write_text(f"{mount}\n")
    proc = hermetic(["--hide", str(source), "--objects", f"{source / '.git' / 'objects'}:{mount}"],
                    ["git", "-C", str(source), "log", "--format=%s"], env=GIT_ENV)
    assert proc.returncode != 0
