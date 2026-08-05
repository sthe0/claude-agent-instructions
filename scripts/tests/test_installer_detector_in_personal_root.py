"""The SessionStart detector runs in the root it detects, and reports where the
agent can hear it.

Two halves, matching the two halves of the change.

INSTALLER. ``install-reminder-hooks.sh`` adds exactly one hook to the prune-only
personal root — ``hook-canon-guard-wired-check.py`` — and no other. The negative
half is the load-bearing one: it is what makes a future "just install
everything" change fail here rather than silently import enforcement into a
personal session, which is a deliberately cut order element.

HOOK. The detector's report moves from stderr to stdout (the channel a
SessionStart hook's output reaches the model's context through), gains an
unconditional status line naming the live harness root, and gains a branch: in a
PERSONAL root it says one quiet line when the session is doing system work and
NOTHING AT ALL otherwise.

The test seam is the trap here, so it is spelled out once. ``agent_home()`` reads
``$CLAUDE_CONFIG_DIR`` first and ``harness_config_root()`` reads only
``$CLAUDE_CONFIG_DIR``, so setting that one variable COLLAPSES both accessors to
a single value and makes every differing-roots direction vacuously green. The
differing-roots fixtures therefore UNSET ``CLAUDE_CONFIG_DIR``, set
``CLAUDE_AGENT_HOME``, and point ``HOME`` at tmp_path — ``harness_config_root()``
falls through to ``Path.home() / ".claude"``, which follows ``$HOME``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from lib import hook_wiring  # noqa: E402

HOOK_SCRIPT = SCRIPTS_DIR / "hook-canon-guard-wired-check.py"
INSTALLER = SCRIPTS_DIR / "install-reminder-hooks.sh"
DETECTOR_BASENAME = "hook-canon-guard-wired-check.py"
GUARD_BASENAME = "hook-guard-canon-readonly.py"
GUARD_PATH = str(SCRIPTS_DIR / GUARD_BASENAME)
BANNER = "GATE-BEARING HOOKS ARE NOT FULLY WIRED"


def _load_hook_module():
    """The detector as an importable module, so the system-work predicate can be
    called directly rather than inferred from the hook's output."""
    spec = importlib.util.spec_from_file_location("_canon_guard_wired_check", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── The installer half ───────────────────────────────────────────────────────

def _shell_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "CLAUDE_AGENT_HOME": str(agent_home),
        "AGENTCTL_EDIT_LEDGER": str(tmp_path / "edit-log.jsonl"),
        "CLAUDE_INSTRUCTIONS_REPO": str(REPO_ROOT),
    }


def _personal_settings(env, content):
    d = Path(env["HOME"]) / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "settings.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def _wired_basenames(data):
    return {
        os.path.basename((h.get("command") or "").split()[0])
        for groups in (data.get("hooks") or {}).values()
        for grp in groups
        for h in grp.get("hooks", []) or []
    }


def _run_installer(env):
    return subprocess.run(
        [str(INSTALLER)], env=env, capture_output=True, text=True, timeout=60,
    )


def test_installer_adds_the_detector_to_the_personal_root(tmp_path):
    env = _shell_env(tmp_path)
    settings = _personal_settings(env, {"hooks": {"SessionStart": []}})

    proc = _run_installer(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    commands = [
        h["command"]
        for grp in data["hooks"]["SessionStart"]
        for h in grp.get("hooks", []) or []
    ]
    assert any(DETECTOR_BASENAME in c for c in commands), commands


def test_installer_adds_no_gate_bearing_hook_to_the_personal_root(tmp_path):
    """The negative half. A change that widens the exemption — or drops it and
    installs DESIRED wholesale into the prune-only root — fails here."""
    env = _shell_env(tmp_path)
    settings = _personal_settings(env, {"hooks": {"SessionStart": []}})

    proc = _run_installer(env)
    assert proc.returncode == 0, proc.stderr

    wired = _wired_basenames(json.loads(settings.read_text(encoding="utf-8")))
    leaked = sorted(b for b, _ in hook_wiring.GATE_BEARING_HOOKS if b in wired)
    assert leaked == [], f"enforcement hooks imported into the personal root: {leaked}"


def test_installer_creates_a_hooks_block_in_a_hooks_less_personal_root(tmp_path):
    """A personal settings.json with no ``hooks`` key at all is the common state,
    not an exotic one, and the pre-existing prune pass `continue`d on it. The
    file-level protections are untouched (see the two tests below); adding a key
    to a file that exists and parses as an object is not creating a file."""
    env = _shell_env(tmp_path)
    settings = _personal_settings(env, {"model": "opus"})

    proc = _run_installer(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus", "unrelated keys must survive"
    assert DETECTOR_BASENAME in " ".join(_wired_basenames(data))


def test_installer_creates_a_hooks_block_when_hooks_is_null_in_personal_root(tmp_path):
    """A personal settings.json with ``"hooks": null`` (key present, JSON null)
    should also be fixed so the detector can be wired. The setdefault call alone
    would not work because it only acts when the key is missing."""
    env = _shell_env(tmp_path)
    settings = _personal_settings(env, {"hooks": None, "model": "opus"})

    proc = _run_installer(env)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus", "unrelated keys must survive"
    assert DETECTOR_BASENAME in " ".join(_wired_basenames(data))


def test_installer_does_not_create_a_missing_personal_settings_file(tmp_path):
    env = _shell_env(tmp_path)
    path = Path(env["HOME"]) / ".claude" / "settings.json"

    proc = _run_installer(env)
    assert proc.returncode == 0, proc.stderr
    assert not path.exists()


def test_installer_does_not_touch_an_unparseable_personal_settings_file(tmp_path):
    env = _shell_env(tmp_path)
    d = Path(env["HOME"]) / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "settings.json"
    path.write_text("{ not json", encoding="utf-8")

    proc = _run_installer(env)
    assert proc.returncode == 0, proc.stderr
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_installer_is_idempotent_on_the_personal_root(tmp_path):
    env = _shell_env(tmp_path)
    settings = _personal_settings(env, {"hooks": {"SessionStart": []}})

    assert _run_installer(env).returncode == 0
    first = settings.read_text(encoding="utf-8")
    assert _run_installer(env).returncode == 0
    assert settings.read_text(encoding="utf-8") == first


# ── The hook half ────────────────────────────────────────────────────────────

def _group(matcher, command):
    g = {} if matcher is None else {"matcher": matcher}
    g["hooks"] = [{"type": "command", "command": command}]
    return g


def _fully_wired_settings():
    """Every gate-bearing hook registered, so the agent-root branch reaches its
    all-clear path. The canon guard needs BOTH PreToolUse chains and a script
    path that exists; the rest need only to appear somewhere."""
    others = [b for b, _ in hook_wiring.GATE_BEARING_HOOKS if b != GUARD_BASENAME]
    return {"hooks": {"PreToolUse": [
        _group("Edit|Write", GUARD_PATH),
        _group("Bash", GUARD_PATH),
    ] + [_group("Bash", str(SCRIPTS_DIR / b)) for b in others]}}


def _venue(tmp_path, *, system_work):
    """A cwd for the hook. With ``system_work``, an ancestor carries the
    ``scripts/agentctl/machine.py`` sentinel; the cwd itself is NESTED under it,
    so a predicate that only looks at its own directory fails."""
    top = tmp_path / "venue"
    if system_work:
        (top / "scripts" / "agentctl").mkdir(parents=True, exist_ok=True)
        (top / "scripts" / "agentctl" / "machine.py").write_text("", encoding="utf-8")
    cwd = top / "a" / "b" / "c"
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


def _roots_env(tmp_path, *, coincide):
    """Roots that genuinely differ (or genuinely coincide) WITHOUT touching
    ``CLAUDE_CONFIG_DIR`` — see the module docstring on why that variable would
    collapse both accessors and make the differing directions vacuous."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    agent_home = (home / ".claude") if coincide else (tmp_path / "agent-home")
    agent_home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env.pop("CLAUDE_CANON_GUARD_SETTINGS", None)
    env["HOME"] = str(home)
    env["CLAUDE_AGENT_HOME"] = str(agent_home)
    return env, home / ".claude", agent_home


def _run_hook(env, cwd):
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        env=env, cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )


def test_managed_policy_member_is_neutral_here():
    """The one chain member these fixtures cannot write. An unreadable machine
    policy would degrade every probe to UNKNOWN and silence the all-clear
    direction's counterpart — correct in production, an unexplained red here."""
    managed = hook_wiring.managed_settings_path()
    if not managed.is_file():
        return
    try:
        data = json.loads(managed.read_text(encoding="utf-8"))
    except Exception:
        raise AssertionError(f"{managed} exists but is unparseable")
    assert isinstance(data, dict) and not data.get("hooks")


def test_i_personal_root_in_a_system_work_venue_speaks_from_a_nested_cwd(tmp_path):
    """(i) The POSITIVE branch of the system-work predicate, which is the whole
    of what this stage delivers for a bare `claude`. Asserted from a NESTED cwd,
    so a predicate that inspects only its own directory fails here."""
    env, harness, agent_home = _roots_env(tmp_path, coincide=False)
    proc = _run_hook(env, _venue(tmp_path, system_work=True))

    assert proc.returncode == 0
    assert proc.stderr == "", proc.stderr
    assert f"[config-root] harness={harness}" in proc.stdout
    assert f"!= agent home {agent_home}" in proc.stdout
    assert "claude-agent" in proc.stdout
    assert BANNER not in proc.stdout, "the personal root gets the quiet line, not the banner"


def test_ii_agent_root_all_clear_emits_the_root_line_on_stdout(tmp_path):
    """(ii) The case that caused the correction: today this path is entirely
    silent, so an agent that measured the root once has nothing to correct it."""
    env, harness, _ = _roots_env(tmp_path, coincide=True)
    (harness / "settings.json").write_text(
        json.dumps(_fully_wired_settings()), encoding="utf-8")

    proc = _run_hook(env, _venue(tmp_path, system_work=False))

    assert proc.returncode == 0
    assert proc.stderr == "", proc.stderr
    assert proc.stdout.strip() == f"[config-root] harness={harness} (= agent home)"


def test_iii_spelling_tracks_whether_the_roots_coincide(tmp_path):
    """(iii) Both spellings, so a hardcoded one cannot pass."""
    env, harness, _ = _roots_env(tmp_path, coincide=True)
    (harness / "settings.json").write_text(
        json.dumps(_fully_wired_settings()), encoding="utf-8")
    same = _run_hook(env, _venue(tmp_path, system_work=False))
    assert "(= agent home)" in same.stdout
    assert "!= agent home" not in same.stdout


def test_iii_spelling_when_the_roots_differ(tmp_path):
    env, harness, agent_home = _roots_env(tmp_path, coincide=False)
    differ = _run_hook(env, _venue(tmp_path, system_work=True))
    assert f"(!= agent home {agent_home})" in differ.stdout


def test_iv_the_problem_banner_also_lands_on_stdout(tmp_path):
    """(iv) The report itself is relocated, not merely the new line."""
    env, harness, _ = _roots_env(tmp_path, coincide=True)
    (harness / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [_group("Edit|Write", "/nope/other.py")]}}),
        encoding="utf-8")

    proc = _run_hook(env, _venue(tmp_path, system_work=False))

    assert proc.returncode == 0
    assert proc.stderr == "", proc.stderr
    assert BANNER in proc.stdout
    assert "NOT registered" in proc.stdout


def test_v_personal_root_outside_a_system_work_venue_is_byte_silent(tmp_path):
    """(v) The FALSE branch of the same predicate (i) pins TRUE. The PAIR is
    what makes the predicate load-bearing — a constant-false predicate passes
    (v) alone, and a constant-true one passes (i) alone."""
    env, _, _ = _roots_env(tmp_path, coincide=False)
    proc = _run_hook(env, _venue(tmp_path, system_work=False))

    assert proc.returncode == 0
    assert proc.stdout == "", proc.stdout
    assert proc.stderr == "", proc.stderr


def test_vi_the_predicate_is_true_in_this_test_files_own_directory():
    """(vi) The fixture nobody synthesized. (i) and (v) are both driven by a tree
    this module builds, so a sentinel path misspelled CONSISTENTLY in fixture and
    predicate passes both and ships today's silence in the real tree. This
    module lives at <repo>/scripts/tests/, whose real ancestors carry the real
    scripts/agentctl/machine.py — a misspelling fails here and only here."""
    mod = _load_hook_module()
    assert mod.in_system_work_venue(Path(__file__).resolve().parent) is True


def test_vi_the_predicate_is_false_outside_any_such_tree(tmp_path):
    mod = _load_hook_module()
    assert mod.in_system_work_venue(tmp_path) is False


def test_the_status_line_survives_an_unreadable_settings_file(tmp_path):
    """Which root is live does not depend on the settings file parsing, so an
    unreadable file makes the wiring REPORT unknown but never the ROOT. The
    early return stays for the report; the status line moved above it."""
    env, harness, _ = _roots_env(tmp_path, coincide=True)
    (harness / "settings.json").write_text("{ not json", encoding="utf-8")

    proc = _run_hook(env, _venue(tmp_path, system_work=False))

    assert proc.returncode == 0
    assert proc.stderr == "", proc.stderr
    assert proc.stdout.strip() == f"[config-root] harness={harness} (= agent home)"
    assert BANNER not in proc.stdout


def test_the_quiet_line_survives_a_hooks_less_personal_root(tmp_path):
    """The personal-root branch decides before reading settings, so the common
    state — a personal root with no settings.json at all — still speaks. A
    fixture that added one to go green would have dropped the line for every
    real hooks-less personal root."""
    env, harness, _ = _roots_env(tmp_path, coincide=False)
    assert not (harness / "settings.json").exists()

    proc = _run_hook(env, _venue(tmp_path, system_work=True))

    assert proc.returncode == 0
    assert "[config-root] harness=" in proc.stdout
    assert "claude-agent" in proc.stdout
