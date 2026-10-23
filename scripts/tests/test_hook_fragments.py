"""The hook-fragment seam of install-reminder-hooks.sh (scripts/lib/hook_fragments.py).

Every test drives the real installer by subprocess against a fake HOME and agent
home, like test_install_reminder_hooks_two_file_prune.py.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
INSTALLER = SCRIPTS_DIR / "install-reminder-hooks.sh"
CORE_PREFIX = str(SCRIPTS_DIR) + "/"


class Box:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.agent = tmp_path / "agent-home"
        self.plugins = self.agent / "hook-plugins"
        self.plugins.mkdir(parents=True)
        self.home.mkdir()
        self.settings = self.agent / "settings.json"
        self.record = self.agent / "hook-plugins.installed.json"
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "CLAUDE_AGENT_HOME": str(self.agent),
            "AGENTCTL_EDIT_LEDGER": str(tmp_path / "edit-log.jsonl"),
            "CLAUDE_INSTRUCTIONS_REPO": str(REPO_ROOT),
        }

    def run(self):
        proc = subprocess.run(
            [str(INSTALLER)], env=self.env, capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, proc.stderr
        return proc

    def seed(self, data):
        self.settings.write_text(json.dumps(data) + "\n")

    def entries(self, event="PreToolUse", matcher="Bash"):
        data = json.loads(self.settings.read_text())
        return [
            h["command"]
            for g in data.get("hooks", {}).get(event, [])
            if g.get("matcher") == matcher
            for h in g["hooks"]
        ]

    def core_commands(self):
        data = json.loads(self.settings.read_text())
        return sorted(
            h["command"]
            for gs in data["hooks"].values()
            for g in gs
            for h in g["hooks"]
            if CORE_PREFIX in h["command"]
        )

    def fragment_dir(self, name, scripts=("gate.py",)):
        d = self.tmp / name
        (d / "scripts").mkdir(parents=True)
        for s in scripts:
            (d / "scripts" / s).write_text("#!/bin/sh\nexit 0\n")
        return d

    def fragment(self, directory, commands, fname="frag.json", link=None, timeout=5):
        hooks = [{"type": "command", "command": c, "timeout": timeout} for c in commands]
        body = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks}]}}
        path = directory / fname
        path.write_text(json.dumps(body))
        if link:
            os.symlink(path, self.plugins / link)
        return path


def test_install_expands_placeholder_through_symlink_and_is_idempotent(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json")
    box.run()
    want = f"python3 {d.resolve()}/scripts/gate.py"
    assert box.entries().count(want) == 1
    assert "@FRAGMENT_DIR@" not in box.settings.read_text()
    box.run()
    assert box.entries().count(want) == 1


def test_noop_rerun_is_byte_identical_and_writes_no_backup(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json")
    box.run()
    before = box.settings.read_bytes()
    record = box.record.read_bytes()
    (box.agent / "settings.json.bak").unlink()
    box.run()
    assert box.settings.read_bytes() == before
    assert box.record.read_bytes() == record
    assert not (box.agent / "settings.json.bak").exists()


def test_no_fragments_leaves_no_record_and_core_rows_unchanged(tmp_path):
    box = Box(tmp_path)
    box.run()
    assert not box.record.exists()
    assert "hook-plugins" not in box.settings.read_text()
    assert box.core_commands()


def test_record_never_enters_settings_and_backup_is_written(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json")
    box.seed({})
    box.run()
    assert "installed" not in json.loads(box.settings.read_text())
    assert (box.agent / "settings.json.bak").exists()
    record = json.loads(box.record.read_text())
    assert list(record) == ["org.json"]
    assert record["org.json"][0][:2] == ["PreToolUse", "Bash"]


def test_hand_hook_kept_and_removal_takes_only_owned_entries(tmp_path):
    box = Box(tmp_path)
    box.seed(
        {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/opt/hand/keep-me.sh"}]}]}}
    )
    d = box.fragment_dir("org")
    link = box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json")
    box.run()
    core = box.core_commands()
    assert "/opt/hand/keep-me.sh" in box.entries()
    (box.plugins / "org.json").unlink()
    box.run()
    assert box.entries().count("/opt/hand/keep-me.sh") == 1
    assert f"python3 {d.resolve()}/scripts/gate.py" not in box.entries()
    assert box.core_commands() == core
    assert link.exists()


def test_identical_hand_command_is_not_owned_and_survives_removal(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    cmd = f"python3 {d.resolve()}/scripts/gate.py"
    box.seed({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": cmd}]}]}})
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json")
    proc = box.run()
    assert box.entries().count(cmd) == 1
    assert "org.json" in proc.stderr
    assert not box.record.exists()
    (box.plugins / "org.json").unlink()
    box.run()
    assert box.entries().count(cmd) == 1


def test_same_basename_hand_copy_blocks_with_warning(tmp_path):
    box = Box(tmp_path)
    box.seed({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 /opt/old/gate.py"}]}]}})
    d = box.fragment_dir("org")
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json")
    proc = box.run()
    assert box.entries().count("python3 /opt/old/gate.py") == 1
    assert not any(str(d.resolve()) in c for c in box.entries())
    assert "install-reminder-hooks: warning: org.json:" in proc.stderr
    assert "/opt/old/gate.py" in proc.stderr


def test_two_fragments_same_basename_first_in_sorted_order_wins(tmp_path):
    box = Box(tmp_path)
    a, b = box.fragment_dir("a"), box.fragment_dir("b")
    box.fragment(a, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="a.json")
    box.fragment(b, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="b.json")
    proc = box.run()
    cmds = box.entries()
    assert f"python3 {a.resolve()}/scripts/gate.py" in cmds
    assert f"python3 {b.resolve()}/scripts/gate.py" not in cmds
    assert "b.json" in proc.stderr and "a.json" not in proc.stderr


def test_two_same_basename_entries_of_one_fragment_are_both_installed(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(
        d,
        ["python3 @FRAGMENT_DIR@/scripts/gate.py --a", "python3 @FRAGMENT_DIR@/scripts/gate.py --b"],
        link="org.json",
    )
    for _ in range(2):
        box.run()
        cmds = box.entries()
        assert cmds.count(f"python3 {d.resolve()}/scripts/gate.py --a") == 1
        assert cmds.count(f"python3 {d.resolve()}/scripts/gate.py --b") == 1


def test_fragment_entry_twinning_a_core_row_is_blocked(tmp_path):
    box = Box(tmp_path)
    box.run()
    data = json.loads(box.settings.read_text())
    core_cmds = box.core_commands()
    event, matcher, core_cmd = next(
        (e, g.get("matcher"), h["command"])
        for e, gs in data["hooks"].items()
        for g in gs
        for h in g["hooks"]
        if CORE_PREFIX in h["command"]
    )
    d = box.fragment_dir("org", scripts=(os.path.basename(core_cmd),))
    group = {"hooks": [{"type": "command", "command": "python3 @FRAGMENT_DIR@/scripts/" + os.path.basename(core_cmd)}]}
    if matcher is not None:
        group["matcher"] = matcher
    (d / "twin.json").write_text(json.dumps({"hooks": {event: [group]}}))
    os.symlink(d / "twin.json", box.plugins / "twin.json")
    proc = box.run()
    assert box.core_commands() == core_cmds
    assert str(d.resolve()) not in box.settings.read_text()
    assert "twin.json" in proc.stderr


def test_edited_command_and_moved_directory_leave_no_stale_entry(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org", scripts=("gate.py", "gate2.py"))
    path = box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json")
    box.run()
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate2.py"])
    box.run()
    cmds = box.entries()
    assert f"python3 {d.resolve()}/scripts/gate.py" not in cmds
    assert cmds.count(f"python3 {d.resolve()}/scripts/gate2.py") == 1
    moved = tmp_path / "moved"
    os.rename(d, moved)
    (box.plugins / "org.json").unlink()
    os.symlink(moved / path.name, box.plugins / "org.json")
    box.run()
    cmds = box.entries()
    assert not any(str(d.resolve()) in c for c in cmds)
    assert cmds.count(f"python3 {moved.resolve()}/scripts/gate2.py") == 1


def test_timeout_of_an_owned_entry_follows_the_fragment(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json", timeout=5)
    box.run()
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], timeout=9)
    box.run()
    data = json.loads(box.settings.read_text())
    timeouts = [
        h["timeout"]
        for g in data["hooks"]["PreToolUse"]
        for h in g["hooks"]
        if h["command"].endswith("scripts/gate.py") and str(d.resolve()) in h["command"]
    ]
    assert timeouts == [9]


def test_broken_fragments_are_skipped_with_warning(tmp_path):
    box = Box(tmp_path)
    (box.plugins / "bad.json").write_text("{ not json\n")
    d = box.fragment_dir("org")
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/missing.py"], fname="missing.json", link="missing.json")
    good = box.fragment_dir("good")
    box.fragment(good, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="good.json")
    proc = box.run()
    assert "bad.json" in proc.stderr and "missing.json" in proc.stderr
    assert "good.json" not in proc.stderr
    assert "missing.py" not in box.settings.read_text()
    assert f"python3 {good.resolve()}/scripts/gate.py" in box.entries()


def test_fragment_broken_after_a_good_run_keeps_its_owned_entries(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    path = box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json")
    box.run()
    want = f"python3 {d.resolve()}/scripts/gate.py"
    path.write_text("{ not json\n")
    proc = box.run()
    assert box.entries().count(want) == 1
    assert "org.json" in proc.stderr
    assert "org.json" in json.loads(box.record.read_text())
    (box.plugins / "org.json").unlink()
    box.run()
    assert want not in box.entries()


def test_removal_drops_emptied_group_and_event(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    body = {"hooks": {"Notification": [{"hooks": [
        {"type": "command", "command": "python3 @FRAGMENT_DIR@/scripts/gate.py"}]}]}}
    (d / "n.json").write_text(json.dumps(body))
    os.symlink(d / "n.json", box.plugins / "n.json")
    box.run()
    assert any(str(d.resolve()) in json.dumps(g) for g in json.loads(box.settings.read_text())["hooks"]["Notification"])
    (box.plugins / "n.json").unlink()
    box.run()
    assert str(d.resolve()) not in box.settings.read_text()


def test_personal_root_gets_no_fragments(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json")
    personal = box.home / ".claude"
    personal.mkdir()
    (personal / "settings.json").write_text("{}\n")
    box.run()
    assert str(d.resolve()) not in (personal / "settings.json").read_text()


def test_doctor_check_lists_uninstalled_fragment_entries(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate.py"], link="org.json")
    box.seed({})
    check = [str(SCRIPTS_DIR / "lib" / "hook_fragments.py"), "--check", str(box.agent)]
    before = subprocess.run(["python3", *check], capture_output=True, text=True, timeout=30)
    assert "org.json" in before.stdout
    box.run()
    after = subprocess.run(["python3", *check], capture_output=True, text=True, timeout=30)
    assert after.stdout == ""
