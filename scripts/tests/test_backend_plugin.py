"""Plugin workspace backends: the contract stays tested in Core CI via a FIXTURE.

Core ships exactly one builtin workspace backend — backends/git.sh — and
registry.sh resolves a backend name builtin-first, then from
${CLAUDE_PROJECT_PLUGIN_DIR}/backends/<name>.sh. Every other VCS is a
machine-local PLUGIN. A Core builtin carrying a plugin's name would SHADOW that
plugin on every machine (re-breaking the launcher and violating Core
org-portability), so the builtin set must stay {git} —
test_core_ships_only_the_git_builtin is the permanent guard for that.

To cover the plugin CONTRACT without Core owning any deployment's VCS semantics,
the tests install a MINIMAL backend fixture under a SYNTHETIC name into a tmp
CLAUDE_PROJECT_PLUGIN_DIR (the same slot a real machine symlinks its plugin
into) and prove:
  (a) registry.sh resolves the name to that plugin fixture;
  (b) with no plugin present, resolution FAILS naming both looked-in paths;
  (c) session-isolate.sh dispatches to the plugin fixture (backend-blind);
  (d) with no plugin present, session-isolate.sh degrades <name> -> git and succeeds.
A deployment's real mount behavior stays covered by that deployment's own tests.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_scope import registry  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BUILTIN_BACKENDS_DIR = REPO / "project_entry" / "backends"  # dir holding the tracked builtins
REGISTRY_LIB = REPO / "project_entry" / "registry.sh"
ISOLATE_SH = REPO / "session-isolate.sh"

# A backend name no deployment owns, so the fixture can never collide with (or
# stand in for) a real plugin.
PLUGIN = "othervcs"

# A minimal, hermetic plugin backend: honors CLAUDE_DRY_RUN, never mounts, and
# reports an isolated workspace path at <anchor>_<name> — enough to prove the router
# dispatches to it and to prove the dry-run "report, don't mutate" contract.
_FIXTURE_BACKEND = """#!/usr/bin/env bash
backend_detect() { return 0; }
backend_ensure_workspace() {
  local name="$1" branch="$2" anchor mount
  anchor="${CLAUDE_WORKSPACE_ROOT:-$PWD}"
  mount="${anchor}_${name}"
  if [[ -n "${CLAUDE_DRY_RUN:-}" ]]; then
    printf 'plugin fixture: [dry-run] would create workspace %s on branch %s\\n' "$mount" "$branch" >&2
  else
    mkdir -p "$mount"
  fi
  printf '%s\\n' "$mount"
}
backend_compose() { :; }
"""


def _install_fixture(plugin_dir: Path, name: str = PLUGIN) -> Path:
    """Write the minimal backend into <plugin_dir>/backends/<name>.sh, mirroring the
    slot a real machine symlinks its plugin into. Returns the backend path."""
    backends = plugin_dir / "backends"
    backends.mkdir(parents=True, exist_ok=True)
    backend = backends / f"{name}.sh"
    backend.write_text(_FIXTURE_BACKEND)
    backend.chmod(0o755)
    return backend


def _stub_git(tmp_path: Path, fake_toplevel: Path, calls_log: Path, wt_list: Path) -> Path:
    """A GIT_BIN stub (same shape as test_session_isolate.py): logs calls, answers
    rev-parse/worktree-list, and treats `worktree add` as a recorded no-op — so the
    plugin->git degrade path runs hermetically without touching a real repo."""
    stub = tmp_path / "git-stub"
    stub.write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >>{calls_log}
shift_n=0
[[ "${{1:-}}" == "-C" ]] && shift_n=2
shift $shift_n 2>/dev/null || true
case "$1 $2" in
  "rev-parse --show-toplevel") printf '%s\\n' "{fake_toplevel}" ;;
  "worktree list")             cat {wt_list} ;;
  "worktree add")              : ;;
  *) : ;;
esac
""")
    stub.chmod(0o755)
    return stub


def _scopes_dir(home: Path) -> Path:
    return home / ".claude" / "agentctl" / "scopes"


# ── The permanent guard: git is Core's ONLY builtin backend ──────────────────


def test_core_ships_only_the_git_builtin():
    """Any other builtin would shadow the machine-local plugin of the same name
    (registry.sh resolves builtin-first), re-breaking the launcher on every machine
    that installs that plugin — so the tracked builtin set must stay {git}."""
    builtins = sorted(p.name for p in BUILTIN_BACKENDS_DIR.glob("*.sh"))
    assert builtins == ["git.sh"], (
        f"{BUILTIN_BACKENDS_DIR} ships {builtins}; only git.sh may be a Core builtin. "
        "Every other backend belongs at ${CLAUDE_PROJECT_PLUGIN_DIR}/backends/<name>.sh "
        "(a builtin shadows the real plugin because registry.sh resolves builtin-first)."
    )


# ── (a)/(b) registry.sh resolves the name from the plugin dir, not from Core ──


def test_registry_resolves_name_from_plugin_fixture(tmp_path):
    plugin_dir = tmp_path / "plugins"
    fixture = _install_fixture(plugin_dir)

    env = dict(os.environ)
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(plugin_dir)
    proc = subprocess.run(
        ["bash", "-c", f'source "{REGISTRY_LIB}"; registry_resolve_workspace {PLUGIN}'],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(fixture)


def test_registry_fails_naming_both_paths_when_no_plugin(tmp_path):
    plugin_dir = tmp_path / "empty-plugins"  # no backends/<name>.sh installed
    plugin_dir.mkdir()

    env = dict(os.environ)
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(plugin_dir)
    proc = subprocess.run(
        ["bash", "-c", f'source "{REGISTRY_LIB}"; registry_resolve_workspace {PLUGIN}'],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode != 0
    # The error names BOTH looked-in locations: the Core builtin slot and the plugin slot.
    assert str(BUILTIN_BACKENDS_DIR / f"{PLUGIN}.sh") in proc.stderr
    assert str(plugin_dir / "backends" / f"{PLUGIN}.sh") in proc.stderr


# ── (c)/(d) session-isolate.sh is backend-blind + degrades <name> -> git ─────


def test_session_isolate_dispatches_to_plugin_fixture(tmp_path):
    """detector=<plugin> + the plugin installed → session-isolate.sh sources the plugin
    fixture and re-registers the session's scope at the new workspace root, proving the
    router dispatches by name with no backend-specific branch of its own."""
    home = tmp_path / "home"
    home.mkdir()
    main = tmp_path / "mainline"
    main.mkdir()
    plugin_dir = tmp_path / "plugins"
    _install_fixture(plugin_dir)

    detector = tmp_path / "det-plugin.py"
    detector.write_text(f"print('{PLUGIN} othertracker')\n")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = "/usr/bin:/bin"
    # config_root resolves CLAUDE_CONFIG_DIR/CLAUDE_AGENT_HOME before HOME —
    # strip them so the child derives its root from the tmp HOME, not the
    # developer machine's real isolated root.
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_AGENT_HOME", None)
    env["CLAUDE_CODE_SESSION_ID"] = "s-plugin"
    env["CLAUDE_DRY_RUN"] = "1"
    env["CLAUDE_BACKEND_DETECTOR"] = str(detector)
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(plugin_dir)

    proc = subprocess.run(
        ["bash", str(ISOLATE_SH), "task-name"],
        cwd=str(main), capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr

    expected = str(main) + "_task-name"
    assert proc.stdout.strip().splitlines()[-1] == expected
    assert f"workspace={PLUGIN}" in proc.stderr
    # Dry-run mutated nothing on disk.
    assert not Path(expected).exists()

    rec = registry.load(_scopes_dir(home), "s-plugin")
    assert rec is not None
    assert rec.repo_root == expected
    assert rec.vcs == PLUGIN


def test_session_isolate_degrades_to_git_when_plugin_absent(tmp_path):
    """detector=<plugin> but NO plugin installed → session-isolate.sh degrades to the
    org-neutral git default (session-isolate.sh lines 72-80) and still succeeds. This
    is the org-portability guarantee: a machine without the plugin is never wedged."""
    home = tmp_path / "home"
    home.mkdir()
    fake_toplevel = tmp_path / "myrepo"
    fake_toplevel.mkdir()
    empty_plugins = tmp_path / "empty-plugins"  # plugin deliberately absent
    empty_plugins.mkdir()
    git_calls = tmp_path / "git-calls.log"
    wt_list = tmp_path / "wt-list.txt"
    git_calls.write_text("")
    wt_list.write_text("")

    detector = tmp_path / "det-plugin.py"
    detector.write_text(f"print('{PLUGIN} othertracker')\n")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = "/usr/bin:/bin"
    # config_root resolves CLAUDE_CONFIG_DIR/CLAUDE_AGENT_HOME before HOME —
    # strip them so the child derives its root from the tmp HOME, not the
    # developer machine's real isolated root.
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("CLAUDE_AGENT_HOME", None)
    env["CLAUDE_CODE_SESSION_ID"] = "s-degrade"
    env["CLAUDE_DRY_RUN"] = "1"
    env["CLAUDE_BACKEND_DETECTOR"] = str(detector)
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(empty_plugins)
    env["GIT_BIN"] = str(_stub_git(tmp_path, fake_toplevel, git_calls, wt_list))

    proc = subprocess.run(
        ["bash", str(ISOLATE_SH), "task-name"],
        cwd=str(fake_toplevel), capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "falling back to git" in proc.stderr
    assert "workspace=git" in proc.stderr

    expected = str(tmp_path / "myrepo-task-name")
    assert proc.stdout.strip().splitlines()[-1] == expected
    # Degraded to git, so no mutating git call under dry-run.
    assert "worktree add" not in git_calls.read_text()

    rec = registry.load(_scopes_dir(home), "s-degrade")
    assert rec is not None
    assert rec.repo_root == expected
    assert rec.vcs == "git"
