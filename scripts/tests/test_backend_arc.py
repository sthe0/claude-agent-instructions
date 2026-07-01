"""arc workspace backend: contract stays tested in Core CI via a plugin FIXTURE.

By design arc is a machine-local PLUGIN backend (Yandex-specific), not a Core
builtin: Core ships only backends/git.sh, and registry.sh resolves a backend name
builtin-first then from ${CLAUDE_PROJECT_PLUGIN_DIR}/backends/<name>.sh. A Core
builtin backends/arc.sh would SHADOW the real machine-local plugin on every machine
(re-breaking the launcher and violating Core org-portability), so it must not exist
in the tree — test_no_core_builtin_arc_backend is the permanent guard for that.

To keep the arc CONTRACT covered without Core owning Yandex arc semantics, the tests
install a MINIMAL arc backend fixture into a tmp CLAUDE_PROJECT_PLUGIN_DIR (the same
slot a real machine symlinks the arc plugin into) and prove:
  (a) registry.sh resolves `arc` to that plugin fixture;
  (b) with no plugin present, resolution FAILS naming both looked-in paths;
  (c) session-isolate.sh dispatches to the plugin fixture (backend-blind);
  (d) with no plugin present, session-isolate.sh degrades arc -> git and succeeds.
Real arc mount behavior stays covered by the storage tree's test-arc-startrek.sh.
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
CORE_ARC_SH = REPO / "project_entry" / "backends" / "arc.sh"

# A minimal, hermetic arc plugin backend: honors CLAUDE_DRY_RUN, never mounts, and
# reports an isolated mount path at <anchor>_<name> — enough to prove the router
# dispatches to it and to prove the dry-run "report, don't mutate" contract.
_FIXTURE_ARC_BACKEND = """#!/usr/bin/env bash
backend_detect() { return 0; }
backend_ensure_workspace() {
  local name="$1" branch="$2" anchor mount
  anchor="${CLAUDE_WORKSPACE_ROOT:-$PWD}"
  mount="${anchor}_${name}"
  if [[ -n "${CLAUDE_DRY_RUN:-}" ]]; then
    printf 'arc fixture: [dry-run] would create mount %s on branch %s\\n' "$mount" "$branch" >&2
  else
    mkdir -p "$mount"
  fi
  printf '%s\\n' "$mount"
}
backend_compose() { :; }
"""


def _install_fixture(plugin_dir: Path) -> Path:
    """Write the minimal arc backend into <plugin_dir>/backends/arc.sh, mirroring the
    slot a real machine symlinks its arc plugin into. Returns the backend path."""
    backends = plugin_dir / "backends"
    backends.mkdir(parents=True, exist_ok=True)
    arc = backends / "arc.sh"
    arc.write_text(_FIXTURE_ARC_BACKEND)
    arc.chmod(0o755)
    return arc


def _stub_git(tmp_path: Path, fake_toplevel: Path, calls_log: Path, wt_list: Path) -> Path:
    """A GIT_BIN stub (same shape as test_session_isolate.py): logs calls, answers
    rev-parse/worktree-list, and treats `worktree add` as a recorded no-op — so the
    arc->git degrade path runs hermetically without touching a real repo."""
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


# ── The permanent guard: Core must NOT ship a builtin arc backend ────────────


def test_no_core_builtin_arc_backend():
    """arc is a machine-local plugin, never a Core builtin. A builtin backends/arc.sh
    would shadow the real plugin (registry.sh is builtin-first), re-breaking the
    launcher on every machine — so the tree must not carry it."""
    assert not CORE_ARC_SH.exists(), (
        f"{CORE_ARC_SH} exists as a Core builtin; it must be a machine-local plugin "
        "under ${CLAUDE_PROJECT_PLUGIN_DIR}/backends/arc.sh instead (it shadows the "
        "real plugin because registry.sh resolves builtin-first)."
    )


# ── (a)/(b) registry.sh resolves `arc` from the plugin dir, not from Core ────


def test_registry_resolves_arc_from_plugin_fixture(tmp_path):
    plugin_dir = tmp_path / "plugins"
    fixture = _install_fixture(plugin_dir)

    env = dict(os.environ)
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(plugin_dir)
    proc = subprocess.run(
        ["bash", "-c", f'source "{REGISTRY_LIB}"; registry_resolve_workspace arc'],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(fixture)


def test_registry_arc_fails_naming_both_paths_when_no_plugin(tmp_path):
    plugin_dir = tmp_path / "empty-plugins"  # no backends/arc.sh installed
    plugin_dir.mkdir()

    env = dict(os.environ)
    env["CLAUDE_PROJECT_PLUGIN_DIR"] = str(plugin_dir)
    proc = subprocess.run(
        ["bash", "-c", f'source "{REGISTRY_LIB}"; registry_resolve_workspace arc'],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode != 0
    # The error names BOTH looked-in locations: the Core builtin slot and the plugin slot.
    assert str(BUILTIN_BACKENDS_DIR / "arc.sh") in proc.stderr
    assert str(plugin_dir / "backends" / "arc.sh") in proc.stderr


# ── (c)/(d) session-isolate.sh is backend-blind + degrades arc -> git ────────


def test_session_isolate_dispatches_to_arc_plugin_fixture(tmp_path):
    """detector=arc + the arc plugin installed → session-isolate.sh sources the plugin
    fixture and re-registers the session's scope at the new mount root, proving the
    router dispatches by name with no arc-specific branch of its own."""
    home = tmp_path / "home"
    home.mkdir()
    main = tmp_path / "arcadia"
    main.mkdir()
    plugin_dir = tmp_path / "plugins"
    _install_fixture(plugin_dir)

    detector = tmp_path / "det-arc.py"
    detector.write_text("print('arc startrek')\n")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = "/usr/bin:/bin"
    env["CLAUDE_SESSION_ID"] = "s-arc"
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
    assert "workspace=arc" in proc.stderr
    # Dry-run mutated nothing on disk.
    assert not Path(expected).exists()

    rec = registry.load(_scopes_dir(home), "s-arc")
    assert rec is not None
    assert rec.repo_root == expected
    assert rec.vcs == "arc"


def test_session_isolate_degrades_arc_to_git_when_plugin_absent(tmp_path):
    """detector=arc but NO arc plugin installed → session-isolate.sh degrades to the
    org-neutral git default (session-isolate.sh lines 69-77) and still succeeds. This
    is the org-portability guarantee: a machine without the arc plugin is never wedged."""
    home = tmp_path / "home"
    home.mkdir()
    fake_toplevel = tmp_path / "myrepo"
    fake_toplevel.mkdir()
    empty_plugins = tmp_path / "empty-plugins"  # arc plugin deliberately absent
    empty_plugins.mkdir()
    git_calls = tmp_path / "git-calls.log"
    wt_list = tmp_path / "wt-list.txt"
    git_calls.write_text("")
    wt_list.write_text("")

    detector = tmp_path / "det-arc.py"
    detector.write_text("print('arc startrek')\n")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = "/usr/bin:/bin"
    env["CLAUDE_SESSION_ID"] = "s-degrade"
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
