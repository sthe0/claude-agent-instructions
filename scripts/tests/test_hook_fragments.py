"""The hook-fragment seam of install-reminder-hooks.sh (scripts/lib/hook_fragments.py).

Every test drives the real installer by subprocess against a fake HOME and agent
home, like test_install_reminder_hooks_two_file_prune.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent

REPO_ROOT = SCRIPTS_DIR.parent
INSTALLER = SCRIPTS_DIR / "install-reminder-hooks.sh"
CORE_PREFIX = str(SCRIPTS_DIR) + "/"
CORE_SCRIPT = "hook-skill-first.py"
CORE_COMMAND = CORE_PREFIX + CORE_SCRIPT

GATE = "python3 @FRAGMENT_DIR@/scripts/gate.py"


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

    def hand(self, *commands):
        self.seed({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": c} for c in commands]}]}})

    def hooks(self, event="PreToolUse", matcher="Bash"):
        data = json.loads(self.settings.read_text())
        return [
            h
            for g in data.get("hooks", {}).get(event, [])
            if g.get("matcher") == matcher
            for h in g["hooks"]
        ]

    def entries(self, event="PreToolUse", matcher="Bash"):
        return [h["command"] for h in self.hooks(event, matcher)]

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
        hooks = []
        for c in commands:
            hook = dict(c) if isinstance(c, dict) else {"command": c}
            hook.update(type="command", timeout=timeout)
            hooks.append(hook)
        body = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": hooks}]}}
        path = directory / fname
        path.write_text(json.dumps(body))
        if link:
            os.symlink(path, self.plugins / link)
        return path

    def settle(self):
        """Remove the backup and return the settings bytes a second run must keep."""
        backup = self.agent / "settings.json.bak"
        if backup.exists():
            backup.unlink()
        return self.settings.read_bytes()

    def assert_stable(self, settled):
        self.run()
        assert self.settings.read_bytes() == settled
        assert not (self.agent / "settings.json.bak").exists()


def test_install_expands_placeholder_through_symlink_and_is_idempotent(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, [GATE], link="org.json")
    box.run()
    want = f"python3 {d.resolve()}/scripts/gate.py"
    assert box.entries().count(want) == 1
    assert "@FRAGMENT_DIR@" not in box.settings.read_text()
    box.run()
    assert box.entries().count(want) == 1


def test_noop_rerun_is_byte_identical_and_writes_no_backup(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, [GATE], link="org.json")
    box.run()
    record = box.record.read_bytes()
    settled = box.settle()
    box.assert_stable(settled)
    assert box.record.read_bytes() == record


def test_no_fragments_leaves_no_record_and_core_rows_unchanged(tmp_path):
    box = Box(tmp_path)
    box.run()
    assert not box.record.exists()
    assert "hook-plugins" not in box.settings.read_text()
    assert box.core_commands()
    box.assert_stable(box.settle())
    assert not box.record.exists()


def test_record_never_enters_settings_and_backup_is_written(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, [GATE], link="org.json")
    box.seed({})
    box.run()
    assert "installed" not in json.loads(box.settings.read_text())
    assert (box.agent / "settings.json.bak").exists()
    record = json.loads(box.record.read_text())
    assert record == {
        "org.json": [
            {"event": "PreToolUse", "matcher": "Bash", "command": f"python3 {d.resolve()}/scripts/gate.py"}
        ]
    }


def test_hand_hook_kept_and_removal_takes_only_owned_entries(tmp_path):
    box = Box(tmp_path)
    box.hand("/opt/hand/keep-me.sh")
    d = box.fragment_dir("org")
    link = box.fragment(d, [GATE], link="org.json")
    box.run()
    core = box.core_commands()
    assert "/opt/hand/keep-me.sh" in box.entries()
    (box.plugins / "org.json").unlink()
    box.run()
    assert box.entries().count("/opt/hand/keep-me.sh") == 1
    assert f"python3 {d.resolve()}/scripts/gate.py" not in box.entries()
    assert box.core_commands() == core
    assert link.exists()


def test_identical_hand_command_blocks_with_warning_and_survives_removal(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    cmd = f"python3 {d.resolve()}/scripts/gate.py"
    box.hand(cmd)
    box.fragment(d, [GATE], link="org.json")
    proc = box.run()
    assert box.entries().count(cmd) == 1
    assert "install-reminder-hooks: warning: org.json:" in proc.stderr
    assert not box.record.exists()
    (box.plugins / "org.json").unlink()
    box.run()
    assert box.entries().count(cmd) == 1


def test_same_basename_hand_copy_installs_with_advisory_warning(tmp_path):
    box = Box(tmp_path)
    box.hand("python3 /opt/old/gate.py")
    d = box.fragment_dir("org")
    box.fragment(d, [GATE], link="org.json")
    proc = box.run()
    assert box.entries().count("python3 /opt/old/gate.py") == 1
    assert box.entries().count(f"python3 {d.resolve()}/scripts/gate.py") == 1
    assert "install-reminder-hooks: warning: org.json:" in proc.stderr
    assert "/opt/old/gate.py" in proc.stderr
    assert "may fire twice" in proc.stderr
    box.assert_stable(box.settle())


def test_hand_identical_to_an_owned_entry_loses_only_the_owned_one(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    cmd = f"python3 {d.resolve()}/scripts/gate.py"
    box.fragment(d, [GATE], link="org.json")
    box.run()
    data = json.loads(box.settings.read_text())
    group = next(g for g in data["hooks"]["PreToolUse"] if g.get("matcher") == "Bash")
    group["hooks"].append({"type": "command", "command": cmd})
    box.seed(data)
    proc = box.run()
    assert box.entries().count(cmd) == 1
    assert "org.json" in proc.stderr and "dropped" in proc.stderr
    (box.plugins / "org.json").unlink()
    box.run()
    assert box.entries().count(cmd) == 1


def test_two_fragments_identical_command_first_in_sorted_order_wins(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("shared")
    box.fragment(d, [GATE], fname="a.json", link="a.json")
    box.fragment(d, [GATE], fname="b.json", link="b.json")
    proc = box.run()
    assert box.entries().count(f"python3 {d.resolve()}/scripts/gate.py") == 1
    assert "b.json" in proc.stderr and "fragment a.json" in proc.stderr
    assert list(json.loads(box.record.read_text())) == ["a.json"]


def test_one_fragment_listing_the_same_command_twice_installs_it_once(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, [GATE, GATE], link="org.json")
    for _ in range(2):
        proc = box.run()
        assert box.entries().count(f"python3 {d.resolve()}/scripts/gate.py") == 1
        assert "org.json" in proc.stderr and "twice" in proc.stderr


def test_two_same_basename_entries_of_one_fragment_are_both_installed(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, [GATE + " --a", GATE + " --b"], link="org.json")
    for _ in range(2):
        proc = box.run()
        cmds = box.entries()
        assert cmds.count(f"python3 {d.resolve()}/scripts/gate.py --a") == 1
        assert cmds.count(f"python3 {d.resolve()}/scripts/gate.py --b") == 1
        assert "org.json" not in proc.stderr


def test_launcher_forms_are_exact_strings_and_both_install(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    commands = [
        "python3.12 -m pkg.one",
        "python3.13 -m pkg.one",
        "uv run one.py",
        "uv run two.py",
        "npx tool-one",
        "npx tool-two",
        "FOO=1 python3 -m pkg.x",
        "FOO=2 python3 -m pkg.x",
        "$SOME_DIR/gate-one.sh",
        "$SOME_DIR/gate-two.sh",
    ]
    box.fragment(d, commands, link="org.json")
    for _ in range(2):
        box.run()
        assert all(box.entries().count(c) == 1 for c in commands)


def test_fragment_entry_with_the_basename_of_a_core_row_installs_and_core_row_survives(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org", scripts=(CORE_SCRIPT,))
    twin = f"python3 {d.resolve()}/scripts/{CORE_SCRIPT}"
    box.fragment(d, [f"python3 @FRAGMENT_DIR@/scripts/{CORE_SCRIPT}"], link="org.json", timeout=7)
    box.seed({})
    proc = box.run()
    assert box.entries().count(CORE_COMMAND) == 1 and box.entries().count(twin) == 1
    assert "org.json" in proc.stderr and CORE_SCRIPT in proc.stderr

    data = json.loads(box.settings.read_text())
    for g in data["hooks"]["PreToolUse"]:
        if g.get("matcher") == "Bash":
            g["hooks"] = [h for h in g["hooks"] if h["command"] != CORE_COMMAND]
    box.seed(data)
    box.run()
    assert box.entries().count(CORE_COMMAND) == 1
    assert box.entries().count(twin) == 1
    assert [h["timeout"] for h in box.hooks() if h["command"] == twin] == [7]
    box.assert_stable(box.settle())


def test_core_row_added_after_an_identical_fragment_entry_wins(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, [CORE_COMMAND], link="org.json")
    box.seed({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": CORE_COMMAND, "timeout": 99}]}]}})
    box.record.write_text(json.dumps({"org.json": [
        {"event": "PreToolUse", "matcher": "Bash", "command": CORE_COMMAND}]}))
    proc = box.run()
    assert box.entries().count(CORE_COMMAND) == 1
    assert [h["timeout"] for h in box.hooks() if h["command"] == CORE_COMMAND] == [5]
    assert "org.json" in proc.stderr and "dropped" in proc.stderr


def test_id_carrying_entry_is_updated_in_place_and_id_never_reaches_settings(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org", scripts=("gate.py", "gate2.py"))
    box.fragment(d, [{"id": "guard", "command": GATE}], link="org.json")
    box.hand("/opt/hand/after.sh")
    box.run()
    data = json.loads(box.settings.read_text())
    group = next(g for g in data["hooks"]["PreToolUse"] if g.get("matcher") == "Bash")
    group["hooks"].append({"type": "command", "command": "/opt/hand/last.sh"})
    box.seed(data)
    order = box.entries()
    box.fragment(d, [{"id": "guard", "command": "python3 @FRAGMENT_DIR@/scripts/gate2.py"}])
    box.run()
    new = f"python3 {d.resolve()}/scripts/gate2.py"
    old = f"python3 {d.resolve()}/scripts/gate.py"
    after = box.entries()
    assert new in after and old not in after
    assert after == [new if c == old else c for c in order]
    assert '"id"' not in box.settings.read_text()
    assert json.loads(box.record.read_text())["org.json"][0]["id"] == "guard"
    box.assert_stable(box.settle())


def test_adding_and_removing_an_id_keeps_the_installed_entry(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    want = f"python3 {d.resolve()}/scripts/gate.py"
    box.fragment(d, [GATE], link="org.json")
    box.run()
    for body in ({"id": "guard", "command": GATE}, {"command": GATE}):
        box.fragment(d, [body])
        proc = box.run()
        assert box.entries().count(want) == 1
        assert "not installed" not in proc.stderr and "dropped" not in proc.stderr
        entry = json.loads(box.record.read_text())["org.json"][0]
        assert entry.get("id") == body.get("id")
    box.assert_stable(box.settle())


def test_new_id_entry_taking_the_old_command_of_a_retargeted_one_keeps_both(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org", scripts=("gate.py", "gate2.py"))
    gate2 = "python3 @FRAGMENT_DIR@/scripts/gate2.py"
    box.fragment(d, [{"id": "a", "command": GATE}], link="org.json")
    box.run()
    box.fragment(d, [{"id": "b", "command": GATE}, {"id": "a", "command": gate2}])
    box.run()
    base = f"python3 {d.resolve()}/scripts/"
    assert sorted(c for c in box.entries() if c.startswith(base)) == [base + "gate.py", base + "gate2.py"]
    box.assert_stable(box.settle())


def test_collision_warning_names_the_holder_of_a_hand_entry(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.hand(f"python3 {d.resolve()}/scripts/gate.py")
    box.fragment(d, [GATE], link="org.json")
    proc = box.run()
    assert "no fragment owns" in proc.stderr


def test_dangling_fragment_symlink_keeps_entries_and_warns(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    want = f"python3 {d.resolve()}/scripts/gate.py"
    path = box.fragment(d, [GATE], link="org.json")
    box.run()
    moved = path.with_name("moved.json")
    path.rename(moved)
    proc = box.run()
    assert box.entries().count(want) == 1
    assert "org.json" in proc.stderr and "skipped, owned entries kept" in proc.stderr
    assert "org.json" in json.loads(box.record.read_text())
    moved.rename(path)
    box.assert_stable(box.settle())


def test_missing_record_leaves_installed_entries_unowned(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    cmd = f"python3 {d.resolve()}/scripts/gate.py"
    box.fragment(d, [GATE], link="org.json")
    box.run()
    box.record.unlink()
    proc = box.run()
    assert box.entries().count(cmd) == 1
    assert "org.json" in proc.stderr
    assert not box.record.exists()
    (box.plugins / "org.json").unlink()
    box.run()
    assert box.entries().count(cmd) == 1


def test_unparsable_record_skips_the_fragment_pass(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org", scripts=("gate.py", "gate2.py"))
    box.fragment(d, [GATE], link="org.json")
    box.run()
    cmd = f"python3 {d.resolve()}/scripts/gate.py"
    box.record.write_text("{ not json\n")
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/gate2.py"])
    settings_before = box.settings.read_bytes()
    proc = box.run()
    assert box.settings.read_bytes() == settings_before
    assert box.record.read_text() == "{ not json\n"
    assert cmd in box.entries() and "gate2.py" not in box.settings.read_text()
    assert "hook-plugins.installed.json" in proc.stderr


def test_malformed_record_entries_are_unparsable_too(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, [GATE], link="org.json")
    box.record.write_text(json.dumps({"org.json": [["PreToolUse", "Bash", "x"]]}))
    proc = box.run()
    assert str(d.resolve()) not in box.settings.read_text()
    assert "hook-plugins.installed.json" in proc.stderr


def test_edited_command_and_moved_directory_leave_no_stale_entry(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org", scripts=("gate.py", "gate2.py"))
    path = box.fragment(d, [GATE], link="org.json")
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
    box.fragment(d, [GATE], link="org.json", timeout=5)
    box.run()
    box.fragment(d, [GATE], timeout=9)
    box.run()
    cmd = f"python3 {d.resolve()}/scripts/gate.py"
    assert [h["timeout"] for h in box.hooks() if h["command"] == cmd] == [9]


def test_broken_fragments_are_skipped_with_warning(tmp_path):
    box = Box(tmp_path)
    (box.plugins / "bad.json").write_text("{ not json\n")
    d = box.fragment_dir("org")
    box.fragment(d, ["python3 @FRAGMENT_DIR@/scripts/missing.py"], fname="missing.json", link="missing.json")
    good = box.fragment_dir("good")
    box.fragment(good, [GATE], link="good.json")
    proc = box.run()
    assert "bad.json" in proc.stderr and "missing.json" in proc.stderr
    assert "good.json" not in proc.stderr
    assert "missing.py" not in box.settings.read_text()
    assert f"python3 {good.resolve()}/scripts/gate.py" in box.entries()


def test_fragment_broken_after_a_good_run_keeps_its_owned_entries(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    path = box.fragment(d, [GATE], link="org.json")
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
        {"type": "command", "command": GATE}]}]}}
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
    box.fragment(d, [GATE], link="org.json")
    personal = box.home / ".claude"
    personal.mkdir()
    (personal / "settings.json").write_text("{}\n")
    box.run()
    assert str(d.resolve()) not in (personal / "settings.json").read_text()


def test_doctor_check_lists_uninstalled_fragment_entries(tmp_path):
    box = Box(tmp_path)
    d = box.fragment_dir("org")
    box.fragment(d, [{"id": "guard", "command": GATE}], link="org.json")
    box.seed({})
    check = [str(SCRIPTS_DIR / "lib" / "hook_fragments.py"), "--check", str(box.agent)]
    before = subprocess.run(["python3", *check], capture_output=True, text=True, timeout=30)
    assert "org.json" in before.stdout
    box.run()
    after = subprocess.run(["python3", *check], capture_output=True, text=True, timeout=30)
    assert after.stdout == ""
