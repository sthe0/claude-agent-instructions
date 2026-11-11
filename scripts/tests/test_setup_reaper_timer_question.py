"""The setup question for the disk-guard timer (offer_reaper_timer in setup-symlinks.sh).

Hermetic: the functions are pulled verbatim out of the real script and run with a stub installer and
a tmp_path agent home; nothing here runs the real installer, systemctl or setup-symlinks.sh.

SETUP_QUESTION_MUTANT=default-no flips the [Y/n] default in the extracted text;
SETUP_QUESTION_MUTANT=readme-note-missing makes the README test read the README without its marked block.
"""
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SETUP_SYMLINKS = REPO / "scripts" / "setup-symlinks.sh"
README = REPO / "README.md"
MUTANT = os.environ.get("SETUP_QUESTION_MUTANT", "")
QUESTION = "Install the disk-guard timer (runs the pressure reaper every minute)? [Y/n]"


def _extract_bash_function(source: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", source, re.MULTILINE | re.DOTALL)
    assert match, f"function {name}() not found"
    return match.group(0)


def _functions() -> str:
    source = SETUP_SYMLINKS.read_text(encoding="utf-8")
    text = "".join(
        _extract_bash_function(source, n)
        for n in ("reaper_timer_have_tty", "reaper_timer_record_decline", "offer_reaper_timer")
    )
    if MUTANT == "default-no":
        assert '""|[Yy]|[Yy][Ee][Ss]) ;;' in text
        text = text.replace('""|[Yy]|[Yy][Ee][Ss]) ;;', "[Yy]|[Yy][Ee][Ss]) ;;")
    return text


class Case:
    def __init__(self, tmp_path: Path, status_exit: int, install_exit: int = 0):
        self.root = tmp_path / "core"
        (self.root / "scripts").mkdir(parents=True)
        self.home = tmp_path / "agent-home"
        self.home.mkdir()
        self.calls = tmp_path / "calls.txt"
        stub = self.root / "scripts" / "install-reaper-timer.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$*" >> "{self.calls}"\n'
            'if [[ "${1:-}" == --status ]]; then echo "status-output"; '
            f"exit {status_exit}; fi\n"
            f'echo "install-output"; exit {install_exit}\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
        self.answer = self.home / "reaper" / "install-answer.toml"

    def run(self, flag: str = "", tty: bool = False, reply: str = "", tmpdir: str = "/sessions/tmp"):
        script = (
            "set -uo pipefail\n"
            f'REPO="{self.root}"\nCLAUDE_AGENT_HOME="{self.home}"\n'
            + (f'TMPDIR="{tmpdir}"\n' if tmpdir else "unset TMPDIR\n")
            + _functions()
            + ("reaper_timer_have_tty() { return 0; }\n" if tty else "")
            + f'offer_reaper_timer "{flag}"\necho "returned=$?"\n'
        )
        return subprocess.run(["bash", "-c", script], input=reply, capture_output=True, text=True)

    def install_calls(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [c for c in self.calls.read_text().splitlines() if c != "--status"]


def test_interactive_enter_installs_with_the_sessions_temp_root(tmp_path):
    c = Case(tmp_path, status_exit=3)
    r = c.run(tty=True, reply="\n")
    assert c.install_calls() == ["--tmpdir /sessions/tmp"], r.stdout + r.stderr
    assert "returned=0" in r.stdout and not c.answer.exists()


def test_interactive_y_installs(tmp_path):
    c = Case(tmp_path, status_exit=3)
    c.run(tty=True, reply="y\n")
    assert c.install_calls() == ["--tmpdir /sessions/tmp"]


def test_interactive_n_records_the_decline_with_its_date(tmp_path):
    c = Case(tmp_path, status_exit=3)
    r = c.run(tty=True, reply="n\n")
    assert c.install_calls() == []
    text = c.answer.read_text()
    assert 'reaper-timer = "declined"' in text and re.search(r"date = \"\d{4}-\d{2}-\d{2}\"", text)
    assert "returned=0" in r.stdout


def test_no_tty_prints_the_question_and_command_and_installs_nothing(tmp_path):
    c = Case(tmp_path, status_exit=3)
    r = c.run(tty=False)
    assert c.install_calls() == []
    assert QUESTION in r.stdout
    assert f"{c.root}/scripts/install-reaper-timer.sh --tmpdir /sessions/tmp" in r.stdout
    assert not c.answer.exists()


def test_real_tty_probe_is_false_without_a_terminal(tmp_path):
    c = Case(tmp_path, status_exit=3)
    r = subprocess.run(
        ["bash", "-c", f'{_extract_bash_function(SETUP_SYMLINKS.read_text(), "reaper_timer_have_tty")}'
                        "reaper_timer_have_tty; echo rc=$?"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert "rc=1" in r.stdout and c.install_calls() == []


def test_advance_flag_yes_installs_without_a_terminal(tmp_path):
    c = Case(tmp_path, status_exit=3)
    c.run(flag="yes", tty=False)
    assert c.install_calls() == ["--tmpdir /sessions/tmp"]


def test_advance_flag_yes_overrides_a_recorded_decline_and_clears_it(tmp_path):
    c = Case(tmp_path, status_exit=3)
    c.answer.parent.mkdir(parents=True)
    c.answer.write_text('reaper-timer = "declined"\n')
    c.run(flag="yes")
    assert c.install_calls() == ["--tmpdir /sessions/tmp"]
    assert not c.answer.exists()


def test_advance_flag_yes_when_installed_does_not_reinstall(tmp_path):
    c = Case(tmp_path, status_exit=0)
    r = c.run(flag="yes")
    assert c.install_calls() == [] and "already installed" in r.stdout


def test_advance_flag_no_records_the_decline(tmp_path):
    c = Case(tmp_path, status_exit=3)
    c.run(flag="no", tty=True)
    assert c.install_calls() == []
    assert 'reaper-timer = "declined"' in c.answer.read_text()


def test_no_user_manager_skips_with_the_reason_and_never_asks(tmp_path):
    c = Case(tmp_path, status_exit=4)
    r = c.run(tty=True, reply="\n")
    assert c.install_calls() == []
    assert "skipped" in r.stdout and "status-output" in r.stdout and QUESTION not in r.stdout


def test_already_installed_is_not_asked(tmp_path):
    c = Case(tmp_path, status_exit=0)
    r = c.run(tty=True, reply="\n")
    assert c.install_calls() == [] and QUESTION not in r.stdout


def test_a_recorded_decline_is_not_asked_again_and_says_how_to_install_later(tmp_path):
    c = Case(tmp_path, status_exit=3)
    c.answer.parent.mkdir(parents=True)
    c.answer.write_text('reaper-timer = "declined"\n')
    r = c.run(tty=True, reply="\n")
    assert c.install_calls() == [] and QUESTION not in r.stdout
    assert "install-reaper-timer.sh" in r.stdout


def test_a_failed_install_prints_its_output_records_nothing_and_returns_zero(tmp_path):
    c = Case(tmp_path, status_exit=3, install_exit=1)
    r = c.run(tty=True, reply="\n")
    assert "install failed (exit 1)" in r.stderr and "install-output" in r.stderr
    assert not c.answer.exists() and "returned=0" in r.stdout


def test_install_without_a_temp_root_passes_no_tmpdir(tmp_path):
    c = Case(tmp_path, status_exit=3)
    c.run(flag="yes", tmpdir="")
    assert c.install_calls() == [""]


def test_the_sandbox_runs_setup_without_the_flag_and_without_a_terminal():
    sandbox = (REPO / "scripts" / "instruction-sandbox.sh").read_text(encoding="utf-8")
    line = next(l for l in sandbox.splitlines() if "core/scripts/setup-symlinks.sh" in l and "setup.log" in l)
    assert "--reaper-timer" not in line
    setup = SETUP_SYMLINKS.read_text(encoding="utf-8")
    assert re.search(r'^offer_reaper_timer "\$REAPER_TIMER_FLAG"$', setup, re.MULTILINE)


def test_readme_note_tells_claude_to_ask_and_install_from_the_canonical_checkout():
    text = README.read_text(encoding="utf-8")
    if MUTANT == "readme-note-missing":
        text = re.sub(r"<!-- reaper-timer-question -->.*?<!-- /reaper-timer-question -->", "", text, flags=re.DOTALL)
    m = re.search(r"<!-- reaper-timer-question -->(.*?)<!-- /reaper-timer-question -->", text, re.DOTALL)
    assert m, "marked README block missing"
    block = m.group(1)
    for needle in ("install-reaper-timer.sh", "--reaper-timer", "installing as the default", "canonical checkout"):
        assert needle in block, needle
