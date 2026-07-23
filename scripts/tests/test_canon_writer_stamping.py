"""Stage 2 of canon-writer-ledger-attribution: every direct-IO canon writer
(2 Python + 4 shell) stamps the ledger's `stamp()` primitive (Stage 1) with a
distinguishing tool marker, so a canon file written outside the Edit/Write
hook chokepoint is still attributable to a session.

Each test drives the writer for real (import + call, or a real subprocess for
the shell scripts) and asserts a ledger row with the expected `tool` marker
appears — not just that the writer didn't crash. Every shell-writer test pins
HOME / CLAUDE_AGENT_HOME / AGENTCTL_EDIT_LEDGER (and AGENTCTL_SCRATCH_ROOTS
where relevant) explicitly, so the suite holds on any machine rather than the
ambient one this happened to be authored on (cf. test_ledger_stamp.py's
$TMPDIR finding).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from agentctl import edit_ledger

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent


def _load_record_experience():
    path = SCRIPTS_DIR / "record-experience.py"
    spec = importlib.util.spec_from_file_location("record_experience_stamping", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_stamp_memory_dates():
    path = SCRIPTS_DIR / "stamp-memory-dates.py"
    spec = importlib.util.spec_from_file_location("stamp_memory_dates_stamping", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rec = _load_record_experience()
smd = _load_stamp_memory_dates()


def _rows(ledger_path):
    return list(edit_ledger.read_records(ledger_path))


def _tools(ledger_path):
    return [r["tool"] for r in _rows(ledger_path)]


# ---------------------------------------------------------------------------
# record-experience.py — cmd_new / cmd_extend / cmd_set_last_verified /
# cmd_ticket, each in-process against a real ledger path, --session threaded.
# ---------------------------------------------------------------------------

def _new_args(tmp_path, ledger_path, **overrides):
    base = dict(
        scope="global", project_dir=None, date="2026-07-23",
        slug="canon-writer-test", title="T", description="d",
        confirmed_by="ok", difficulty="x", order="o", criterion="c",
        context_where="W", plan="P", refs=[], tier=None,
        context_label="initial", plan_file=None, cost=None, self_critique=None,
        justify_new=None, session="the-session",
    )
    base.update(overrides)
    args = SimpleNamespace(**base)
    monkey_exp_dir = tmp_path / "experience"
    return args, monkey_exp_dir


def test_cmd_new_stamps_leaf_and_subindex(tmp_path, monkeypatch):
    ledger_path = tmp_path / "edit-log.jsonl"
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(ledger_path))
    exp_dir = tmp_path / "experience"
    monkeypatch.setattr(rec, "experience_dir", lambda scope, project_dir: exp_dir)

    args, _ = _new_args(tmp_path, ledger_path)
    rc = rec.cmd_new(args)
    assert rc == 0

    tools = _tools(ledger_path)
    assert "record-experience:new" in tools
    assert "record-experience:subindex" in tools
    rows = _rows(ledger_path)
    leaf_row = next(r for r in rows if r["tool"] == "record-experience:new")
    assert leaf_row["file"] == str((exp_dir / "2026-07-23-canon-writer-test.md").resolve())
    assert leaf_row["session_id"] == "the-session"


def test_cmd_new_without_session_stamps_with_empty_session(tmp_path, monkeypatch):
    # SimpleNamespace without a `session` attr must not raise (mirrors the
    # pre-existing test_promote_scan.py / test_tier_field.py call shape).
    ledger_path = tmp_path / "edit-log.jsonl"
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(ledger_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    exp_dir = tmp_path / "experience"
    monkeypatch.setattr(rec, "experience_dir", lambda scope, project_dir: exp_dir)

    args, _ = _new_args(tmp_path, ledger_path)
    del args.session
    rc = rec.cmd_new(args)
    assert rc == 0
    rows = _rows(ledger_path)
    assert rows  # writer path did not silently skip stamping
    assert all(r["session_id"] == "" for r in rows)


def test_cmd_extend_stamps_leaf(tmp_path, monkeypatch):
    ledger_path = tmp_path / "edit-log.jsonl"
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(ledger_path))
    leaf = tmp_path / "leaf.md"
    leaf.write_text(
        "---\nname: x\nschema: difficulty/v1\n---\n\n# T\n\n## Contexts\n\n### 2026-01-01 — old\n",
        encoding="utf-8")
    a = SimpleNamespace(
        leaf=str(leaf), date="2026-07-23", context_label="new", context_where="W",
        plan="P", common=None, variations=None, session="the-session",
    )
    rc = rec.cmd_extend(a)
    assert rc == 0
    rows = _rows(ledger_path)
    assert len(rows) == 1
    assert rows[0]["tool"] == "record-experience:extend"
    assert rows[0]["file"] == str(leaf.resolve())


def test_cmd_set_last_verified_stamps_leaf(tmp_path, monkeypatch):
    ledger_path = tmp_path / "edit-log.jsonl"
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(ledger_path))
    leaf = tmp_path / "leaf.md"
    leaf.write_text("---\nname: x\ncreated: 2026-06-01\nlast_verified: 2026-06-01\n---\nbody\n",
                    encoding="utf-8")
    a = SimpleNamespace(leaf=str(leaf), date="2026-07-23", session="the-session")
    rc = rec.cmd_set_last_verified(a)
    assert rc == 0
    rows = _rows(ledger_path)
    assert len(rows) == 1
    assert rows[0]["tool"] == "record-experience:set-last-verified"
    assert rows[0]["file"] == str(leaf.resolve())


def test_cmd_ticket_stamps_leaf_and_subindex(tmp_path, monkeypatch):
    ledger_path = tmp_path / "edit-log.jsonl"
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(ledger_path))
    exp_dir = tmp_path / "experience"
    monkeypatch.setattr(rec, "experience_dir", lambda scope, project_dir: exp_dir)

    a = SimpleNamespace(
        scope="global", project_dir=None, date="2026-07-23", slug="tix",
        title="T", description="d", confirmed_by="ok", refs=[],
        difficulty="x", order="o", criterion="c", context_where="W", plan="P",
        ticket="ABC-1", ticket_url=None, context_label="initial",
        distill=None, tier=None, session="the-session",
    )
    rc = rec.cmd_ticket(a)
    assert rc == 0
    tools = _tools(ledger_path)
    assert "record-experience:ticket" in tools
    assert "record-experience:subindex" in tools


# ---------------------------------------------------------------------------
# stamp-memory-dates.py — --apply stamps each changed leaf; dry-run stamps
# nothing (mutation-proof: the dry-run branch never reaches the write site).
# ---------------------------------------------------------------------------

def test_stamp_memory_dates_apply_stamps_and_dry_run_does_not(tmp_path, monkeypatch):
    ledger_path = tmp_path / "edit-log.jsonl"
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(ledger_path))
    leaf = tmp_path / "leaves" / "note.md"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("# Note\n\nbody\n", encoding="utf-8")

    monkeypatch.setattr(smd, "iter_leaves", lambda scope, project_dir: iter([leaf]))

    rc = smd.main(["--scope", "global"])  # dry-run (default)
    assert rc == 0
    assert _rows(ledger_path) == []

    rc = smd.main(["--scope", "global", "--apply"])
    assert rc == 0
    rows = _rows(ledger_path)
    assert len(rows) == 1
    assert rows[0]["tool"] == "stamp-memory-dates"
    assert rows[0]["file"] == str(leaf.resolve())


# ---------------------------------------------------------------------------
# Shell writers — real subprocess, sandboxed HOME / CLAUDE_AGENT_HOME /
# AGENTCTL_EDIT_LEDGER. Each asserts the writer's own script resolves and
# calls ITS OWN copy of edit-ledger.py (this worktree's), not a canonical
# install elsewhere — the self-relative resolution the plan requires.
# ---------------------------------------------------------------------------

def _shell_env(tmp_path, ledger_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "CLAUDE_AGENT_HOME": str(agent_home),
        "AGENTCTL_EDIT_LEDGER": str(ledger_path),
    }


def test_apply_settings_sh_stamps_target(tmp_path):
    ledger_path = tmp_path / "edit-log.jsonl"
    env = _shell_env(tmp_path, ledger_path)
    target = Path(env["CLAUDE_AGENT_HOME"]) / "settings.json"

    proc = subprocess.run(
        [str(SCRIPTS_DIR / "apply-settings.sh")],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    rows = _rows(ledger_path)
    assert any(r["tool"] == "script:apply-settings" and r["file"] == str(target.resolve())
              for r in rows)


def test_install_reminder_hooks_sh_stamps_target(tmp_path):
    ledger_path = tmp_path / "edit-log.jsonl"
    env = _shell_env(tmp_path, ledger_path)
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(REPO_ROOT)
    target = Path(env["CLAUDE_AGENT_HOME"]) / "settings.json"

    proc = subprocess.run(
        [str(SCRIPTS_DIR / "install-reminder-hooks.sh")],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    rows = _rows(ledger_path)
    assert any(r["tool"] == "script:install-reminder-hooks" and r["file"] == str(target.resolve())
              for r in rows)


def test_install_reminder_hooks_sh_no_op_second_run_does_not_stamp_again(tmp_path):
    ledger_path = tmp_path / "edit-log.jsonl"
    env = _shell_env(tmp_path, ledger_path)
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(REPO_ROOT)

    subprocess.run([str(SCRIPTS_DIR / "install-reminder-hooks.sh")],
                   env=env, capture_output=True, text=True, timeout=30)
    n_after_first = len(_rows(ledger_path))
    assert n_after_first > 0

    proc2 = subprocess.run([str(SCRIPTS_DIR / "install-reminder-hooks.sh")],
                           env=env, capture_output=True, text=True, timeout=30)
    assert proc2.returncode == 0
    assert len(_rows(ledger_path)) == n_after_first  # idempotent no-op: no extra stamp


def test_install_reminder_hooks_sh_stamps_via_self_relative_stamp_repo_not_repo(tmp_path):
    """Discriminating control for the $STAMP_REPO vs $REPO split.

    The plan requires the worktree copy to resolve `edit_ledger` through ITS OWN
    tree (`$STAMP_REPO`, self-relative), not through the canonical `$REPO`
    (`CLAUDE_INSTRUCTIONS_REPO`), which may be a different checkout. The two other
    shell tests always set the two paths to the same directory, so they would
    still pass if the stamp resolved via `$REPO` — they do not prove the split.
    Here the paths DIVERGE: the invoked script lives in this worktree (real
    `agentctl` on its self-relative path), while `CLAUDE_INSTRUCTIONS_REPO` points
    at a minimal repo that has a valid `lib/config-root.sh` but deliberately NO
    `agentctl` package. A correct self-relative `STAMP_REPO` still stamps; reverting
    it to `$REPO` would make the in-heredoc `from agentctl import edit_ledger`
    fail and the script exit non-zero, so this test goes RED on that mutation.
    """
    ledger_path = tmp_path / "edit-log.jsonl"
    env = _shell_env(tmp_path, ledger_path)

    # Canonical $REPO: a valid config-root.sh (so the `source` succeeds) but no
    # agentctl package, so a stamp can only resolve through the self-relative
    # $STAMP_REPO of the invoked worktree script.
    canonical = tmp_path / "canonical-repo"
    (canonical / "scripts" / "lib").mkdir(parents=True)
    (canonical / "scripts" / "lib" / "config-root.sh").write_text(
        (SCRIPTS_DIR / "lib" / "config-root.sh").read_text(encoding="utf-8"),
        encoding="utf-8")
    assert not (canonical / "scripts" / "agentctl").exists()
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(canonical)
    target = Path(env["CLAUDE_AGENT_HOME"]) / "settings.json"

    proc = subprocess.run(
        [str(SCRIPTS_DIR / "install-reminder-hooks.sh")],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr  # self-relative STAMP_REPO resolved edit_ledger

    rows = _rows(ledger_path)
    assert any(r["tool"] == "script:install-reminder-hooks" and r["file"] == str(target.resolve())
              for r in rows), rows


def test_set_context_cap_sh_stamps_base_and_live(tmp_path):
    ledger_path = tmp_path / "edit-log.jsonl"
    env = _shell_env(tmp_path, ledger_path)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "settings").mkdir(parents=True)
    for name in ("edit-ledger.py", "set-context-cap.sh", "apply-settings.sh"):
        dest = repo / "scripts" / name
        dest.write_text((SCRIPTS_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")
        dest.chmod(0o755)
    import shutil as _sh
    _sh.copytree(SCRIPTS_DIR / "agentctl", repo / "scripts" / "agentctl")
    _sh.copytree(SCRIPTS_DIR / "lib", repo / "scripts" / "lib")
    (repo / "settings" / "base.json").write_text("{}\n", encoding="utf-8")
    env["CLAUDE_INSTRUCTIONS_REPO"] = str(repo)

    live_settings = Path(env["CLAUDE_AGENT_HOME"]) / "settings.json"
    live_settings.write_text("{}\n", encoding="utf-8")

    proc = subprocess.run(
        [str(repo / "scripts" / "set-context-cap.sh"), "300000"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    rows = _rows(ledger_path)
    tools_for_base = [r for r in rows if r["tool"] == "script:set-context-cap"
                      and r["file"] == str((repo / "settings" / "base.json").resolve())]
    tools_for_live = [r for r in rows if r["tool"] == "script:set-context-cap"
                      and r["file"] == str(live_settings.resolve())]
    assert tools_for_base, rows
    assert tools_for_live, rows


def test_setup_project_memory_sh_stamps_stub_memory_md(tmp_path):
    ledger_path = tmp_path / "edit-log.jsonl"
    env = _shell_env(tmp_path, ledger_path)
    project = tmp_path / "myproject"
    project.mkdir()

    proc = subprocess.run(
        [str(SCRIPTS_DIR / "setup-project-memory.sh"), str(project)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    target = project / ".claude" / "agent-memory" / "MEMORY.md"
    assert target.exists()
    rows = _rows(ledger_path)
    assert any(r["tool"] == "script:setup-project-memory" and r["file"] == str(target.resolve())
              for r in rows)
